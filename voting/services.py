import requests
from django.conf import settings
import uuid
import random
import string

def initialize_paystack_payment(email, amount, candidate_id):
    # Generate a unique reference for this transaction
    reference = str(uuid.uuid4())
    
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "email": email,
        "amount": int(amount * 100),  # PayStack expects amount in kobo/cents
        "reference": reference,
        "callback_url": "https://flexyvotes.onrender.com/vote/success/",,
        "metadata": {
            "candidate_id": candidate_id,
            "voter_email": email
        }
    }
    
    response = requests.post(url, json=data, headers=headers)
    res_data = response.json()    
    
    print("PAYSTACK RESPONSE:", res_data)  

    if res_data.get('status'):
        return res_data['data']['authorization_url'], reference
    return None, None

    
def initialize_ticket_payment(email, amount, ticket_id, quantity):
    # Generate 6-character ref (2 letters + 4 numbers, e.g., AB1234)
    letters = ''.join(random.choices(string.ascii_uppercase, k=2))
    numbers = ''.join(random.choices(string.digits, k=4))
    reference = f"TK-{letters}{numbers}" # Added 'TK-' prefix for PayStack compatibility
    
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "email": email,
        "amount": int(amount * 100),
        "reference": reference,
        "callback_url": "https://flexyvotes.onrender.com/vote/success/",
        "metadata": {
            "type": "ticket_purchase",
            "ticket_id": ticket_id,
            "quantity": quantity
        }
    }
    
    response = requests.post(url, json=data, headers=headers)
    res_data = response.json()
    
    if res_data.get('status'):
        return res_data['data']['authorization_url'], reference
    return None, None
    reference = str(uuid.uuid4())
    
    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "email": email,
        "amount": int(amount * 100),
        "reference": reference,
        "callback_url": "http://127.0.0.1:8000/vote/success/",
        "metadata": {
            "type": "ticket_purchase",
            "ticket_id": ticket_id,
            "quantity": quantity
        }
    }
    
    response = requests.post(url, json=data, headers=headers)
    res_data = response.json()
    
    if res_data.get('status'):
        return res_data['data']['authorization_url'], reference
    return None, None