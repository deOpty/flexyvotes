from django.urls import path
from . import views

urlpatterns = [
    path('ussd/callback/', views.ussd_callback, name='ussd_callback'),
    path('', views.home, name='home'),
    path('event/<int:event_id>/', views.event_detail, name='event_detail'),
    path('event/<int:event_id>/analytics/', views.event_analytics, name='event_analytics'),
    path('event/<int:event_id>/edit/', views.edit_event, name='edit_event'),
    path('event/<int:event_id>/add-candidate/', views.add_candidate, name='add_candidate'),
    path('event/<int:event_id>/add-category/', views.add_category, name='add_category'),
    path('event/<int:event_id>/generate-codes/', views.generate_codes, name='generate_codes'),
    path('event/<int:event_id>/download-codes/', views.download_codes, name='download_codes'),
    path('event/<int:event_id>/clear-codes/', views.clear_codes, name='clear_codes'),
    path('event/<int:event_id>/upload-csv/', views.upload_student_csv, name='upload_csv'),
    path('vote/<int:candidate_id>/', views.initiate_vote, name='initiate_vote'),
    path('vote/success/', views.vote_success, name='vote_success'),
    path('vote/code/<int:candidate_id>/', views.cast_vote_with_code, name='cast_vote_with_code'),
    path('webhook/paystack/', views.paystack_webhook, name='paystack_webhook'),
    path('event/<int:event_id>/live-counts/', views.live_vote_counts, name='live_counts'),
    path('category/<int:category_id>/edit/', views.edit_category, name='edit_category'),
    path('candidate/<int:candidate_id>/edit/', views.edit_candidate, name='edit_candidate'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('register/', views.register_view, name='register'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/create/', views.create_event, name='create_event'),
    path('event/<int:event_id>/approve/', views.approve_event, name='approve_event'),
    path('store/', views.store_view, name='store'),
    path('contact/', views.contact_view, name='contact'),
    path('dashboard/store/', views.manage_store, name='manage_store'),
    path('dashboard/store/add/', views.add_product, name='add_product'),
    path('dashboard/store/edit/<int:product_id>/', views.edit_product, name='edit_product'),
    path('dashboard/store/add-category/', views.add_product_category, name='add_product_category'),
    
    # Tickets
    path('tickets/', views.tickets_view, name='tickets'),
    path('event/<int:event_id>/tickets/', views.event_tickets_view, name='event_tickets'),
    path('event/<int:event_id>/create-ticket/', views.create_ticket, name='create_ticket'),
    path('buy-ticket/<int:ticket_id>/', views.buy_ticket, name='buy_ticket'),
    path('ticket/success/', views.ticket_success, name='ticket_success'),
    path('ticket/send-email/', views.send_ticket_email, name='send_ticket_email'),
    path('event/<int:event_id>/guestlist/', views.event_guestlist, name='event_guestlist'),
    path('event/<int:event_id>/download-guestlist/', views.download_guestlist, name='download_guestlist'),
    path('ticket/<int:ticket_id>/edit/', views.edit_ticket, name='edit_ticket'),
    path('ticket/<int:ticket_id>/delete/', views.delete_ticket, name='delete_ticket'),
    
    # Scanner & Verify
    path('verify-ticket/', views.verify_ticket_view, name='verify_ticket'),
    path('event/<int:event_id>/scanner/', views.event_scanner, name='event_scanner'), 
    path('event/<int:event_id>/process-scan/', views.process_scan, name='process_scan'),
    path('retrieve-ticket/', views.retrieve_ticket_view, name='retrieve_ticket'), 

    path('event/<int:event_id>/retrieve-code/', views.retrieve_voting_code, name='retrieve_voting_code'),

    path('event/<int:event_id>/bulk-add/', views.bulk_add_candidates, name='bulk_add_candidates'),
    path('event/<int:event_id>/upload-codes-csv/', views.upload_codes_csv, name='upload_codes_csv'),

    #Ballot Validation and Casting
    path('event/<int:event_id>/validate-ballot/', views.validate_ballot_code, name='validate_ballot'),
    path('event/<int:event_id>/cast-ballot/', views.cast_ballot, name='cast_ballot'),
]