import logging
import uuid
import random
import string
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _paystack_initialize(email, amount, reference, metadata):
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "email": email,
        "amount": int(amount * 100),  # PayStack expects amount in kobo/pesewas
        "reference": reference,
        "callback_url": f"{settings.SITE_URL}/vote/success/",
        "metadata": metadata,
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=15)
        response.raise_for_status()
        res_data = response.json()
    except requests.RequestException:
        logger.exception("PayStack initialization request failed")
        return None, None

    if res_data.get('status'):
        return res_data['data']['authorization_url'], reference
    logger.warning("PayStack initialization was rejected: %s", res_data.get('message'))
    return None, None


def initialize_paystack_payment(email, amount, candidate_id):
    reference = str(uuid.uuid4())
    metadata = {
        "candidate_id": candidate_id,
        "voter_email": email,
    }
    return _paystack_initialize(email, amount, reference, metadata)


def verify_paystack_transaction(reference):
    """
    Confirms a transaction's status directly with PayStack's servers.

    The success/callback pages must never trust a client-supplied
    reference on its own to mark a payment as successful - a user can
    reach that page without ever completing payment. This performs the
    authoritative server-to-server check.
    """
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        res_data = response.json()
    except requests.RequestException:
        logger.exception("PayStack verification request failed for reference %s", reference)
        return False

    return bool(res_data.get('status')) and res_data.get('data', {}).get('status') == 'success'


def initialize_ticket_payment(email, amount, ticket_id, quantity):
    # Generate 6-character ref (2 letters + 4 numbers, e.g., TK-AB1234)
    letters = ''.join(random.choices(string.ascii_uppercase, k=2))
    numbers = ''.join(random.choices(string.digits, k=4))
    reference = f"TK-{letters}{numbers}"
    metadata = {
        "type": "ticket_purchase",
        "ticket_id": ticket_id,
        "quantity": quantity,
    }
    return _paystack_initialize(email, amount, reference, metadata)
