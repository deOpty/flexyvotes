from django.urls import path

from . import views_console as console
from . import views_portal as portal
from . import views_public as public
from . import views_voter as voter

app_name = 'elections'

urlpatterns = [
    # Voter flow (secret ballot)
    path('e/<int:event_id>/vote/', voter.vote_start, name='vote_start'),
    path('e/<int:event_id>/vote/verify/', voter.vote_otp, name='vote_otp'),
    path('e/<int:event_id>/vote/ballot/', voter.vote_ballot, name='vote_ballot'),
    path('e/<int:event_id>/vote/review/', voter.vote_review, name='vote_review'),
    path('e/<int:event_id>/vote/receipt/', voter.vote_receipt, name='vote_receipt'),
    path('e/<int:event_id>/vote/signout/', voter.vote_logout, name='vote_logout'),
    path('e/<int:event_id>/register/', voter.register, name='vote_register'),

    # Public information
    path('e/<int:event_id>/candidates/', public.candidates, name='candidates'),
    path('e/<int:event_id>/candidates/<int:candidate_id>/', public.candidate_profile, name='candidate_profile'),
    path('e/<int:event_id>/dispute/', public.file_dispute, name='file_dispute'),
    path('results/', public.results_index, name='results_index'),
    path('results/<int:event_id>/', public.results, name='results'),
    path('results/<int:event_id>/results.json', public.results_api, name='results_json'),
    path('verify/<int:event_id>/', public.verify, name='verify'),
    path('verify/<int:event_id>/bundle.json', public.verify_bundle, name='verify_bundle'),

    # Election console
    path('console/elections/', console.election_list, name='console_list'),
    path('console/elections/<int:event_id>/', console.overview, name='console_overview'),
    path('console/elections/<int:event_id>/transition/', console.do_transition, name='console_transition'),
    path('console/elections/<int:event_id>/settings/', console.settings_view, name='console_settings'),
    path('console/elections/<int:event_id>/ballot/', console.ballot, name='console_ballot'),
    path('console/elections/<int:event_id>/ballot/preview/', console.ballot_preview, name='console_ballot_preview'),
    path('console/elections/<int:event_id>/candidates/<int:candidate_id>/invite/', portal.invite_candidate,
         name='console_candidate_invite'),
    path('console/elections/<int:event_id>/voters/', console.voters, name='console_voters'),
    path('console/elections/<int:event_id>/voters/add/', console.voter_add, name='console_voter_add'),
    path('console/elections/<int:event_id>/voters/bulk/', console.voters_bulk, name='console_voters_bulk'),
    path('console/elections/<int:event_id>/voters/export.csv', console.voters_export, name='console_voters_export'),
    path('console/elections/<int:event_id>/voters/<int:voter_id>/', console.voter_action, name='console_voter_action'),
    path('console/elections/<int:event_id>/eligibility/', console.eligibility, name='console_eligibility'),
    path('console/elections/<int:event_id>/results/', console.results_view, name='console_results'),
    path('console/elections/<int:event_id>/results/action/', console.results_action, name='console_results_action'),
    path('console/elections/<int:event_id>/results/export.<str:fmt>', console.results_export, name='console_results_export'),
    path('console/elections/<int:event_id>/trustees/', console.trustees, name='console_trustees'),
    path('console/elections/<int:event_id>/integrity/', console.integrity_view, name='console_integrity'),
    path('console/elections/<int:event_id>/integrity/action/', console.integrity_action, name='console_integrity_action'),
    path('console/elections/<int:event_id>/disputes/<int:dispute_id>/', console.dispute_detail, name='console_dispute'),
    path('console/elections/<int:event_id>/incidents/<int:incident_id>/', console.incident_detail, name='console_incident'),
    path('console/elections/<int:event_id>/evidence/<int:item_id>/', console.evidence_download, name='console_evidence'),
    path('console/elections/<int:event_id>/audit/', console.audit_log, name='console_audit'),
    path('console/elections/<int:event_id>/monitor/', console.monitor, name='console_monitor'),
    path('console/elections/<int:event_id>/monitor/data/', console.monitor_data, name='console_monitor_data'),
    path('console/organizations/<int:org_id>/constituencies/', console.constituencies, name='constituencies'),
]

portal_patterns = ([
    path('', portal.portal_home, name='home'),
    path('<int:candidate_id>/', portal.portal_candidate, name='candidate'),
    path('accept/<str:token>/', portal.accept_invitation, name='accept'),
], 'portal')
