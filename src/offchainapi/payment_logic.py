# Copyright (c) The Libra Core Contributors
# SPDX-License-Identifier: Apache-2.0

from .business import BusinessForceAbort, BusinessValidationFailure
from .protocol_command import ProtocolCommand
from .errors import OffChainErrorCode
from .command_processor import CommandProcessor
from .payment import Status, PaymentObject, StatusObject
from .payment_command import PaymentCommand, PaymentLogicError
from .asyncnet import NetworkException
from .shared_object import SharedObject
from .status_logic import STATUS_HEIGHTS
from .libra_address import LibraAddress, LibraAddressError
from .utils import get_unique_string

import asyncio
import logging
import json


class PaymentProcessorNoProgress(Exception):
    pass


class PaymentProcessorRemoteError(Exception):
    pass


logger = logging.getLogger(name='libra_off_chain_api.payment_logic')


class PaymentProcessor(CommandProcessor):
    ''' The logic to process a payment from either side.

    The processor checks commands as they are received from the other
    VASP. When a command from the other VASP is successful it is
    passed on to potentially lead to a further command. It is also
    notified of sequenced commands that failed, and the error that
    lead to that failure.

    Crash-recovery strategy: The processor must only process each
    command once. For this purpose the Executor passes commands
    in the order they have been sequenced by the lower-level
    protocol on each channel, and does so only once for each command
    in the sequence for each channel.

    The Processor must store those commands, and ensure they have
    all been suitably processed upon a potential crash and recovery.
    '''

    def __init__(self, business, storage_factory, loop=None):
        self.business = business

        # Asyncio support
        self.loop = loop
        self.net = None

        # The processor state -- only access through event loop to prevent
        # mutlithreading bugs.
        self.storage_factory = storage_factory

        root = storage_factory.make_dir(self.business.get_my_address())
        processor_dir = storage_factory.make_dir('processor', root=root)
        # map from reference_id to latest version id
        self.reference_id_index = storage_factory.make_dict(
            'reference_id_index', str, processor_dir)

        # This is the primary store of shared objects.
        # It maps version numbers -> objects.
        self.object_store = storage_factory.make_dict(
            'object_store', PaymentObject, root=processor_dir)

        # Allow mapping a set of future to payment reference_id outcomes
        # Once a payment has an outcome (ready_for_settlement, abort, or command exception)
        # notify the appropriate futures of the result. These do not persist
        # crashes since they are run-time objects.

        # Mapping: payment reference_id -> List of futures.
        self.outcome_futures = {}

        # Storage for debug futures list
        self.futs = []

    def set_network(self, net):
        ''' Assigns a concrete network for this command processor to use. '''
        assert self.net is None
        self.net = net

    # ------ Machinery for supporting async Business context ------

    async def process_command_failure_async(
            self, other_address, command, seq, error):
        ''' Process any command failures from either ends of a channel.'''
        logger.error(
            f'(other:{other_address.as_str()}) Command #{seq} Failure: {error} ({error.message})'
        )

        # If this is our own command, that just failed, we should update
        # the outcome:
        try:
            if command.origin != other_address:
                logger.error(
                    f'Command with {other_address.as_str()}.#{seq}'
                    f' Trigger outcome.')

                # try to construct a payment.
                payment = command.get_payment(self.object_store)
                self.set_payment_outcome_exception(
                                payment.reference_id,
                                PaymentProcessorRemoteError(error))
            else:
                logger.error(
                    f'Command with {other_address.as_str()}.#{seq}'
                    f' Error on other VASPs command.')
        except Exception:
            logger.error(
                f'Command with {other_address.as_str()}.#{seq}'
                f' Cannot recover payment or reference_id'
            )

        return

    async def process_command_success_async(self, other_address, command, seq):
        """ The asyncronous command processing logic.

        Checks all incomming commands from the other VASP, and determines if
        any new commands need to be issued from this VASP in response.

        Args:
            other_address (LibraAddress):  The other VASP address in the
                channel that received this command.
            command (PaymentCommand): The current payment command.
            seq (int): The sequence number of the payment command.
        """
        # To process commands we should have set a network
        if self.net is None:
            raise RuntimeError(
                'Setup a processor network to process commands.'
            )

        # Update the outcome of the payment
        payment = command.get_payment(self.object_store)
        self.set_payment_outcome(payment)

        # If there is no registered obligation to process there is no
        # need to process this command. We log here an error, which
        # might be due to a bug.
        other_address_str = other_address.as_str()

        logger.info(f'(other:{other_address_str}) Process Command #{seq}')

        try:
            command_ctx = await self.business.payment_pre_processing(
                other_address, seq, command, payment)

            # Only respond to commands by other side.
            if command.origin == other_address:

                # Determine if we should inject a new command.
                new_payment = await self.payment_process_async(
                    payment, ctx=command_ctx)

                if new_payment.has_changed():
                    new_cmd = PaymentCommand(new_payment)

                    request = await self.net.sequence_command(
                        other_address, new_cmd
                    )

                    # Attempt to send it to the other VASP.
                    await self.net.send_request(other_address, request)
                else:
                    # Signal to anyone waiting that progress was not made
                    # despite being our turn to make progress. As a result
                    # some extra processing should be done until progress
                    # can be made. Note that if the payment is already done
                    # (as in ready_for_settlement/abort) we have set an outcome
                    # for it, and this will be a no-op.
                    self.set_payment_outcome_exception(
                        payment.reference_id,
                        PaymentProcessorNoProgress())

                    is_receiver = self.business.is_recipient(new_payment)
                    role = ['sender', 'receiver'][is_receiver]

                    logger.debug(
                        f'(me: {role} other:{other_address_str}) No more commands '
                        f'created for Payment lastly with seq num #{seq}'
                        f' {new_payment}'
                    )

        except NetworkException as e:
            logger.warning(
                f'(other:{other_address_str}) Network error: seq #{seq}: {e}'
            )
        except Exception as e:
            logger.error(
                f'(other:{other_address_str}) '
                f'Payment processing error: seq #{seq}: {e}',
                exc_info=True,
            )

    # -------- Machinery for notification for outcomes -------

    async def wait_for_payment_outcome(self, reference_id):
        ''' Returns the payment object with the given a reference_id once the
        object has the sender and/or receiver status set to either
        'ready_for_settlement' or 'abort'.
        '''
        fut = self.loop.create_future()

        if reference_id not in self.outcome_futures:
            self.outcome_futures[reference_id] = []

        # Register this future to call later.
        self.outcome_futures[reference_id] += [fut]

        # Check to see if the payment is already resolved.
        if reference_id in self.reference_id_index:
            payment = self.get_latest_payment_by_ref_id(reference_id)
            self.set_payment_outcome(payment)

        return (await fut)

    def set_payment_outcome(self, payment):
        ''' Updates the list of futures waiting for payment outcomes
            based on the new payment object provided. If sender or receiver
            of the payment object are in settled or abort states, then
            the result is passed on to any waiting futures.
        '''

        # Check if payment is in a final state
        if not ((payment.sender.status.as_status() == Status.ready_for_settlement and \
                payment.receiver.status.as_status() == Status.ready_for_settlement) or \
                payment.sender.status.as_status() == Status.abort or \
                payment.receiver.status.as_status() == Status.abort):
            return

        # Check if anyone is waiting for this payment.
        if payment.reference_id not in self.outcome_futures:
            return

        # Get the futures waiting for an outcome, and delete them
        # from the list of pending futures.
        outcome_futures = self.outcome_futures[payment.reference_id]
        del self.outcome_futures[payment.reference_id]

        # Update the outcome for each of the futures.
        for fut in outcome_futures:
            fut.set_result(payment)

    def set_payment_outcome_exception(self, reference_id, payment_exception):
        # Check if anyone is waiting for this payment.
        if reference_id not in self.outcome_futures:
            return

        # Get the futures waiting for an outcome, and delete them
        # from the list of pending futures.
        outcome_futures = self.outcome_futures[reference_id]
        del self.outcome_futures[reference_id]

        # Update the outcome for each of the futures.
        for fut in outcome_futures:
            fut.set_exception(payment_exception)

    # -------- Implements CommandProcessor interface ---------

    def business_context(self):
        ''' Overrides CommandProcessor. '''
        return self.business

    def check_command(self, my_address, other_address, command):
        ''' Overrides CommandProcessor. '''

        new_payment = command.get_payment(self.object_store)

        # Ensure that the two parties involved are in the VASP channel
        parties = set([
            new_payment.sender.get_onchain_address_encoded_str(),
            new_payment.receiver.get_onchain_address_encoded_str()
        ])

        other_addr_str = other_address.as_str()

        needed_parties = set([
            my_address.as_str(),
            other_addr_str
        ])

        if parties != needed_parties:
            raise PaymentLogicError(
                OffChainErrorCode.payment_wrong_actor,
                f'Wrong Parties: expected {needed_parties} '
                f'but got {str(parties)}'
            )


        # Ensure the originator is one of the VASPs in the channel.
        origin_str = command.get_origin().as_str()
        if origin_str not in parties:
            raise PaymentLogicError(
                OffChainErrorCode.payment_wrong_actor,
                f'Command originates from {origin_str} wrong party')

        # Only check the commands we get from others.
        if origin_str == other_addr_str:
            if command.reads_version_map == []:

                # Check that the reference_id is correct
                # Only do this for the definition of new payments, after that
                # the ref id stays the same.

                ref_id_structure = new_payment.reference_id.split('_')
                if not (len(ref_id_structure) > 1 and ref_id_structure[0] == origin_str):
                    raise PaymentLogicError(
                        OffChainErrorCode.payment_wrong_structure,
                        f'Expected reference_id of the form {origin_str}_XYZ, got: '
                        f'{new_payment.reference_id}'
                    )

                self.check_new_payment(new_payment)
            else:

                # Ensure the payment ref_id stays the same
                old_ref_id, _ = command.reads_version_map[0]
                new_ref_id, _ = command.writes_version_map[0]
                if old_ref_id != new_ref_id:
                    raise PaymentLogicError(
                        OffChainErrorCode.payment_wrong_structure,
                        f'Expected the reference id to not change,'
                        f' got: {old_ref_id} and {new_ref_id}'
                    )

                old_version = command.get_previous_version_number()
                old_payment = self.object_store[old_version]
                self.check_new_update(old_payment, new_payment)

    def process_command(self, other_addr, command,
                        cid, status_success, error=None):
        ''' Overrides CommandProcessor. '''

        other_str = other_addr.as_str()

        # Call the failure handler and exit.
        if not status_success:
            fut = self.loop.create_task(self.process_command_failure_async(
                other_addr, command, cid, error)
            )
            if __debug__:
                self.futs += [fut]
            return fut

        # Creates new objects.
        new_versions = command.get_new_object_versions()
        for version in new_versions:
            obj = command.get_object(version, self.object_store)
            self.object_store[version] = obj

        # Update the Index of Reference ID -> Payment.
        self.store_latest_payment_by_ref_id(command)

        # Spin further command processing in its own task.
        logger.debug(f'(other:{other_str}) Schedule cmd {cid}')
        fut = self.loop.create_task(self.process_command_success_async(
            other_addr, command, cid))

        # Log the futures here to execute them inidividually
        # when testing.
        if __debug__:
            self.futs += [fut]

        return fut

    # -------- Get Payment API commands --------

    def get_latest_payment_by_ref_id(self, ref_id):
        ''' Returns the latest payment with the reference ID provided.'''
        version = self.reference_id_index.try_get(ref_id)
        if version is None:
            raise KeyError(ref_id)
        return self.object_store[version]

    def get_payment_history_by_ref_id(self, ref_id):
        ''' Generator that returns all versions of a
            payment with a given reference ID
            in reverse causal order (newest first). '''
        payment = self.get_latest_payment_by_ref_id(ref_id)
        yield payment

        if payment.previous_version is not None:
            p_version = payment.previous_version
            payment = self.object_store[p_version]
            yield payment

    def store_latest_payment_by_ref_id(self, command):
        ''' Internal command to update the payment index '''
        payment = command.get_payment(self.object_store)

        # Update the Index of Reference ID -> Payment.
        ref_id = payment.reference_id

        # Write the new payment to the index of payments by
        # reference ID to support they GetPaymentAPI.
        payment_version = self.reference_id_index.try_get(ref_id)
        if payment_version:
            # We check that the previous version is present.
            # If so we update it with the new one.
            dependencies_versions = command.get_dependencies()
            if payment_version in dependencies_versions:
                self.reference_id_index[ref_id] = payment.version
        else:
            self.reference_id_index[ref_id] = payment.version

    # ----------- END of CommandProcessor interface ---------

    def check_signatures(self, payment):
        ''' Utility function that checks all signatures present for validity.

        Throws a BusinessValidationFailure exception if the recipient signature is present but incorrect.
        '''
        business = self.business
        is_sender = business.is_sender(payment)
        other_actor = payment.receiver if is_sender else payment.sender

        if is_sender and 'recipient_signature' in payment:
            business.validate_recipient_signature(payment)

    def check_new_payment(self, new_payment):
        ''' Checks a diff for a new payment from the other VASP, and returns
            a valid payemnt. If a validation error occurs, then an exception
            is thrown.

            NOTE: the VASP may be the RECEIVER of the new payment, for example
            for person to person payment initiated by the sender. The VASP
            may also be the SENDER for the payment, such as in cases where a
            merchant is charging an account, a refund, or a standing order.`

            The only real check is that that status for the VASP that has
            not created the payment must be none, to allow for checks and
            potential aborts. However, KYC information on both sides may
            be included by the other party, and should be checked.
        '''
        business = self.business
        is_receipient = business.is_recipient(new_payment)

        role = ['sender', 'receiver'][is_receipient]

        if not self.good_initial_status(new_payment, is_receipient):
            raise PaymentLogicError(
                        OffChainErrorCode.payment_wrong_status,
                        f'Sender set receiver status or vice-versa.')

        # Check that the subaddresses are present
        # TODO: catch exceptions into Payment errors

        try:
            sub_send = LibraAddress.from_encoded_str(new_payment.sender.address)
            sub_revr = LibraAddress.from_encoded_str(new_payment.receiver.address)
        except LibraAddressError as e:
            raise PaymentLogicError(
                OffChainErrorCode.payment_invalid_libra_address,
                str(e)
            )

        # TODO: TEST and fix these
        if not sub_send.subaddress_bytes:
            raise PaymentLogicError(
                OffChainErrorCode.payment_invalid_libra_subaddress,
                f'Sender address needs to contain an encoded subaddress, '
                f'but got {sub_send.as_str()}'
            )
        if not sub_revr.subaddress_bytes:
            raise PaymentLogicError(
                OffChainErrorCode.payment_invalid_libra_subaddress,
                f'Receiver address needs to contain an encoded subaddress, '
                f'but got {sub_revr.as_str()}'
            )

        try:
            self.check_signatures(new_payment)
        except BusinessValidationFailure:
            raise PaymentLogicError(
                OffChainErrorCode.payment_wrong_recipient_signature,
                'Recipient signature check failed.'
            )

    def check_new_update(self, payment, new_payment):
        ''' Checks a diff updating an existing payment.

            On success returns the new payment object. All check are fast to
            ensure a timely response (cannot support async operations).
        '''
        business = self.business
        is_receiver = business.is_recipient(new_payment)

        role = ['sender', 'receiver'][is_receiver]
        other_role = ['sender', 'receiver'][role == 'sender']
        myself_actor = payment.data[role]
        myself_actor_new = new_payment.data[role]

        # Ensure nothing on our side was changed by this update.
        if myself_actor != myself_actor_new:
            raise PaymentLogicError(
                OffChainErrorCode.payment_changed_other_actor,
                f'Cannot change {role} information.')

        # Check the status transition is valid.

        other_status_new = new_payment.data[other_role].status.as_status()
        if not self.can_change_status(payment, other_status_new, is_receiver):
            other_status = payment.data[other_role].status.as_status()
            raise PaymentLogicError(
                OffChainErrorCode.payment_wrong_status,
                f'Invalid Status transition: {other_status} -> {other_status_new}')

        try:
            self.check_signatures(new_payment)
        except BusinessValidationFailure:
            raise PaymentLogicError(
                OffChainErrorCode.payment_wrong_recipient_signature,
                'Recipient signature check failed.'
            )

    def payment_process(self, payment):
        ''' A syncronous version of payment processing -- largely
            used for pytests '''
        loop = self.loop
        if self.loop is None:
            loop = asyncio.new_event_loop()
        return loop.run_until_complete(self.payment_process_async(payment))

    def can_change_status(self, payment, new_self, actor_is_sender):
        """ Checks whether an actor can change the status in its PaymentActor
            to a new status accoding to our logic for valid state
            transitions.

        Parameters:
            * payment (PaymentObject): the initial payment we are updating.
            * new_status (Status): the new status we want to transition to.
            * actor_is_sender (bool): whether the actor doing the transition
                is a sender (set False for receiver).

        Returns:
            * bool: True for valid transition and False otherwise.
        """
        if actor_is_sender:
            old_self = payment.sender.status.as_status()
            other = payment.receiver.status.as_status()
        else:
            old_self = payment.receiver.status.as_status()
            other = payment.sender.status.as_status()

        valid = True

        # if other side aborts, self shall abort
        if other == Status.abort:
            valid &= new_self == Status.abort

        # If self has aborted, self shall not change status
        if old_self == Status.abort:
            valid &= old_self == new_self

        # If both are ready_for_settlement, self shall not change status
        if (
            old_self == Status.ready_for_settlement
            and other == Status.ready_for_settlement
        ):
            valid &= old_self == new_self

        # If self is ready_for_settlement, it shall only
        # transit to other status when the other side aborts
        # and it shall only transit to abort
        if (
            old_self == Status.ready_for_settlement
            and other != Status.abort
        ):
            valid &= old_self == new_self

        # Respect ordering of status
        valid &= STATUS_HEIGHTS[new_self] >= STATUS_HEIGHTS[old_self]
        return valid

    def good_initial_status(self, payment, actor_is_sender):
        """ Checks whether a payment has a valid initial status, given
            the role of the actor that created it. Returns a bool set
            to true if it is valid."""

        if actor_is_sender:
            return payment.receiver.status.as_status() == Status.none
        return payment.sender.status.as_status() == Status.none

    async def payment_process_async(self, payment, ctx=None):
        ''' Processes a payment that was just updated, and returns a
            new payment with potential updates. This function may be
            called multiple times for the same payment to support
            async business operations and recovery.

            Must always return a new payment but,
            if there is no update to the new payment
            no new command will be emiited.
        '''
        business = self.business

        is_receiver = business.is_recipient(payment, ctx)
        is_sender = not is_receiver
        role = ['sender', 'receiver'][is_receiver]
        other_role = ['sender', 'receiver'][not is_receiver]

        status = payment.data[role].status.as_status()
        current_status = status
        other_status = payment.data[other_role].status.as_status()

        new_payment = payment.new_version(store=self.object_store)

        abort_code = None
        abort_msg = None

        try:
            await business.payment_initial_processing(payment, ctx)

            if status == Status.abort or (
                status == Status.ready_for_settlement and
                other_status == Status.ready_for_settlement
            ):
                # Nothing more to be done with this payment
                # Return a new payment version with no modification
                # To singnal no changes, and therefore no new command.
                return new_payment

            # We set our status as abort.
            if other_status == Status.abort:
                current_status = Status.abort

                abort_code = 'FOLLOW'
                abort_msg = 'Follows the abort from the other side.'

            if current_status == Status.none:
                await business.check_account_existence(new_payment, ctx)

            # Request more KYC Data or progress the protocol
            if current_status in {Status.none,
                                  Status.needs_kyc_data,
                                  Status.needs_recipient_signature,
                                  Status.soft_match}:

                # Request KYC -- this may be async in case
                # of need for user input
                next_kyc = await business.next_kyc_level_to_request(
                    new_payment, ctx)
                if next_kyc != Status.none:
                    current_status = next_kyc

            # Provide KYC -- this may be async in case
            # of need for user input
            kyc_to_provide = await business.next_kyc_to_provide(
                new_payment, ctx)

            myself_new_actor = new_payment.data[role]

            if Status.needs_kyc_data in kyc_to_provide:
                extended_kyc = await business.get_extended_kyc(new_payment, ctx)
                myself_new_actor.add_kyc_data(extended_kyc)

            if Status.soft_match in kyc_to_provide:
                additional_kyc = await business.get_additional_kyc(new_payment, ctx)
                myself_new_actor.add_additional_kyc_data(additional_kyc)

            if Status.needs_recipient_signature in kyc_to_provide:
                signature = await business.get_recipient_signature(
                    new_payment, ctx)
                new_payment.add_recipient_signature(signature)

            # Check if we have all the KYC we need
            if current_status not in {
                    Status.ready_for_settlement,
                    Status.abort}:
                ready = await business.ready_for_settlement(new_payment, ctx)
                if ready:
                    current_status = Status.ready_for_settlement

        except BusinessForceAbort as e:

            # We cannot abort once we said we are ready_for_settlement
            # or beyond. However we will catch a wrong change in the
            # check when we change status.
            new_payment = payment.new_version(new_payment.version, store=self.object_store)
            current_status = Status.abort

            abort_code = e.code # already a string
            abort_msg = e.message

        except Exception as e:
            # This is an unexpected error, so we need to track it.
            error_ref = get_unique_string()

            logger.error(
                f'[{error_ref}] Error while processing payment {payment.reference_id}'
                ' return error in metadata & abort.')
            logger.exception(e)

            # Only report the error in meta-data
            # & Abort the payment.
            new_payment = payment.new_version(new_payment.version, store=self.object_store)
            current_status = Status.abort

            # TODO: use proper codes and messages on abort.
            abort_code = OffChainErrorCode.payment_vasp_error.value
            abort_msg = f'An unexpected excption was raised by the VASP business logic. Ref: {error_ref}'

        # Do an internal consistency check:
        if not self.can_change_status(payment, current_status, is_sender):
            sender_status = payment.sender.status.as_status()
            receiver_status = payment.receiver.status.as_status()
            raise RuntimeError(
                f'Invalid status transition while processing '
                f'payment {payment.get_version()}: '
                f'(({sender_status}, {receiver_status})) -> {current_status} '
                f'SENDER={is_sender}'
            )

        new_payment.data[role].change_status(
            StatusObject(current_status, abort_code, abort_msg))
        return new_payment
