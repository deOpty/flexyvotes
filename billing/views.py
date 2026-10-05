import io
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone

from core.rbac import has_perm
from core.tenancy import current_organization

from . import service
from .models import Invoice, Plan, Subscription


def _organization(request):
    organization = current_organization(request, 'org.billing')
    if organization is None or not has_perm(request.user, 'org.billing', organization):
        raise PermissionDenied
    return organization


@login_required
def billing_home(request):
    organization = _organization(request)
    subscription = service.get_subscription(organization)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'trial':
                service.start_trial(organization, request.POST.get('plan'), request.user)
                messages.success(request, 'Trial started.')
            elif action == 'change':
                cycle = request.POST.get('cycle') if request.POST.get('cycle') in Subscription.Cycle.values else Subscription.Cycle.MONTHLY
                _, invoice = service.change_plan(organization, request.POST.get('plan'), request.user, cycle,
                                                 request.POST.get('coupon', ''))
                if invoice is not None and invoice.status == Invoice.Status.OPEN:
                    messages.info(request, f'Plan changed. Invoice {invoice.number} is ready for payment.')
                    return redirect('billing:invoice', invoice_id=invoice.pk)
                messages.success(request, 'Plan updated.')
            elif action == 'cancel':
                subscription.cancel_at_period_end = not subscription.cancel_at_period_end
                subscription.save(update_fields=['cancel_at_period_end'])
                messages.success(request, 'Subscription will cancel at period end.' if subscription.cancel_at_period_end
                                 else 'Cancellation withdrawn.')
        except (service.BillingLimitError, Plan.DoesNotExist) as exc:
            messages.error(request, str(exc) or 'Invalid plan.')
        return redirect('billing:home')
    period_start = subscription.current_period_start
    usage = service.usage_summary(organization, period_start, timezone.now() + timedelta(seconds=1))
    return render(request, 'billing/home.html', {
        'organization': organization, 'subscription': subscription, 'plans': Plan.objects.filter(is_public=True),
        'usage': usage, 'invoices': organization.invoices.all()[:24],
        'active_elections': organization.events.exclude(status='ARCHIVED').count(),
    })


@login_required
def invoice_detail(request, invoice_id):
    invoice = get_object_or_404(Invoice.objects.select_related('organization'), pk=invoice_id)
    if not has_perm(request.user, 'org.billing', invoice.organization):
        raise PermissionDenied
    if request.method == 'POST' and request.POST.get('action') == 'pay':
        from payments.service import PaymentError, initiate_invoice_payment

        try:
            payment = initiate_invoice_payment(request, invoice, request.user.email or invoice.organization.contact_email)
            return redirect(payment.authorization_url)
        except PaymentError as exc:
            messages.error(request, exc.message)
        return redirect('billing:invoice', invoice_id=invoice.pk)
    if request.GET.get('format') == 'pdf':
        return _invoice_pdf(invoice)
    return render(request, 'billing/invoice.html', {'invoice': invoice, 'lines': invoice.lines.all()})


def _invoice_pdf(invoice):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, title=f'Invoice {invoice.number}')
    styles = getSampleStyleSheet()
    rows = [['Description', 'Qty', 'Unit', 'Amount']] + [
        [line.description, f'{line.quantity:g}', f'{line.unit_price:.4f}'.rstrip('0').rstrip('.'), f'{line.amount:.2f}']
        for line in invoice.lines.all()]
    rows += [['', '', 'Subtotal', f'{invoice.subtotal:.2f}'], ['', '', 'Discount', f'-{invoice.discount:.2f}'],
             ['', '', f'Levies ({invoice.levy_rate}%)', f'{invoice.levy:.2f}'],
             ['', '', f'VAT ({invoice.vat_rate}%)', f'{invoice.vat:.2f}'], ['', '', 'Total', f'{invoice.total:.2f} {invoice.currency}']]
    table = Table(rows, colWidths=[260, 50, 110, 90])
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#800020')),
                               ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('GRID', (0, 0), (-1, -6), 0.25, colors.grey),
                               ('FONTNAME', (2, -1), (-1, -1), 'Helvetica-Bold')]))
    document.build([
        Paragraph(f'<b>Invoice {invoice.number}</b>', styles['Title']),
        Paragraph(f'{invoice.organization.name} &middot; {invoice.get_status_display()}', styles['Normal']),
        Paragraph(f'Period {invoice.period_start:%d %b %Y} - {invoice.period_end:%d %b %Y} &middot; '
                  f'Issued {invoice.issued_at:%d %b %Y}' if invoice.issued_at else '', styles['Normal']),
        Spacer(1, 12), table])
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{invoice.number}.pdf"'
    return response


app_name = 'billing'
urlpatterns = [
    path('console/billing/', billing_home, name='home'),
    path('console/billing/invoices/<int:invoice_id>/', invoice_detail, name='invoice'),
]
