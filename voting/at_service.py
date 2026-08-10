import africastalking
from django.conf import settings

# Initialize the SDK
africastalking.initialize(
    username=settings.AT_USERNAME,
    api_key=settings.AT_API_KEY
)

# Initialize the payments service
payments = africastalking.Payment

def trigger_mobile_money_checkout(phone_number, amount, product_name, provider):
    """
    Triggers a mobile money checkout prompt to the user's phone.
    """
    try:
        # Africa's Talking requires the phone number in international format without the '+'
        # e.g., 233500000000
        if phone_number.startswith('+'):
            phone_number = phone_number[1:]
            
        # Required metadata for the transaction
        metadata = {
            "note": f"Voting payment of {amount}"
        }
        
        response = payments.mobile_checkout(
            product_name=product_name,
            phone_number=phone_number,
            currency_code="GHS",
            amount=amount,
            metadata=metadata
        )
        return response
    except Exception as e:
        print(f"AT Mobile Money Error: {e}")
        return None