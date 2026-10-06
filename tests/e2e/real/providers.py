"""Providers for the real-data run: a real open embedding model and a ground-truth judge."""

from __future__ import annotations

import time

from yodb.semantic import (
    EmbeddingResult,
    ProviderInfo,
    VerificationResult,
    VerificationUsage,
    VerificationVerdict,
)

# Each proposition is true exactly of the messages whose labelled intent is in its set.
PROPOSITIONS = {
    "the customer is asking when their card will arrive": {"card_arrival", "card_delivery_estimate"},
    "the customer's payment or transfer was declined or failed": {
        "declined_card_payment", "declined_transfer", "failed_transfer", "declined_cash_withdrawal", "top_up_failed"},
    "the customer lost their card or phone, or it was stolen": {
        "lost_or_stolen_card", "lost_or_stolen_phone", "compromised_card", "card_swallowed"},
    "the customer is asking about exchange rates": {
        "exchange_rate", "card_payment_wrong_exchange_rate", "exchange_via_app", "wrong_exchange_rate_for_cash_withdrawal", "exchange_charge"},
    "the customer is complaining about a fee or an unexpected charge": {
        "extra_charge_on_statement", "card_payment_fee_charged", "transfer_fee_charged", "cash_withdrawal_charge",
        "top_up_by_card_charge", "top_up_by_bank_transfer_charge", "transaction_charged_twice"},
    "the customer has a question about verifying their identity": {
        "unable_to_verify_identity", "why_verify_identity", "verify_my_identity", "verify_source_of_funds"},
    "the customer wants to change or has forgotten their PIN or passcode": {"change_pin", "pin_blocked", "passcode_forgotten"},
    "the customer was charged twice for the same transaction": {"transaction_charged_twice"},
}


class FastEmbedder:
    """A real open embedding model, run locally through ONNX (fastembed)."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name)
        probe = next(iter(self._model.embed(["probe"])))
        self.dimensions = len(probe)
        self.info = ProviderInfo("fastembed", model_name, "1")
        self.calls = 0
        self.seconds = 0.0

    def embed_many(self, texts, batch_size: int = 256):
        return [tuple(float(x) for x in vector) for vector in self._model.embed(list(texts), batch_size=batch_size)]

    def embed(self, request):
        started = time.perf_counter()
        vectors = tuple(self.embed_many(request.texts))
        self.calls += 1
        self.seconds += time.perf_counter() - started
        return EmbeddingResult(vectors, self.info, self.dimensions)


class LabelVerifier:
    """A perfect judge: a message satisfies a proposition iff its labelled intent is in the proposition's set.

    It isolates what is being measured (does the *retrieval* find the true matches?) from
    how good a language model is at judging.  Cost is modelled as 0.001 per record judged.
    """

    maximum_batch_size = 100

    def __init__(self, truth: dict[str, str]) -> None:
        self.truth = truth
        self.info = ProviderInfo("labels", "ground-truth", "1")
        self.records_judged = 0
        self.calls = 0

    def verify(self, request):
        intents = PROPOSITIONS[request.proposition]
        self.calls += 1
        self.records_judged += len(request.candidates)
        verdicts = tuple(
            VerificationVerdict(c.logical_id, self.truth.get(c.logical_id) in intents, 0.99 if self.truth.get(c.logical_id) in intents else 0.01)
            for c in request.candidates
        )
        usage = VerificationUsage(model_calls=1, input_tokens=15 * len(verdicts), cost=0.001 * len(verdicts))
        return VerificationResult(verdicts, usage, self.info)
