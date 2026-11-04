from django.template.loader import get_template
from xhtml2pdf import pisa
from django.http import HttpResponse
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from django.core.exceptions import PermissionDenied
from core.plant_section_scope import scoped_queryset
from .models import ShiftAssignment
from .models import ShiftHandover

def render_to_pdf(template_src, context_dict={}):
    template = get_template(template_src)
    html = template.render(context_dict)
    result = HttpResponse(content_type='application/pdf')
    pisa_status = pisa.CreatePDF(html, dest=result)
    if pisa_status.err:
        return HttpResponse(f"We had some errors <pre>{html}</pre>")
    return result

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def export_shift_handover_pdf(request, pk):
    try:
        user = request.user
        handover = scoped_queryset(
            ShiftHandover.objects.select_related(
                'shift', 'outgoing_supervisor', 'incoming_supervisor'
            ).prefetch_related('apparatus_conditions__apparatus'),
            user,
        ).get(pk=pk)
        if not user.is_superuser:
            if not user.has_perm("sc.view_shifthandover"):
                raise PermissionDenied
            is_related = (
                handover.outgoing_supervisor_id == user.pk
                or handover.incoming_supervisor_id == user.pk
                or ShiftAssignment.objects.filter(
                    staff=user,
                    shift=handover.shift,
                    date=handover.date,
                    is_deleted=False,
                ).exists()
            )
            if not is_related:
                raise PermissionDenied

        pdf = render_to_pdf('pdf/handover_template.html', {
            'handover': handover,
            'request': request  # allows use of {{ request.user }} in template
        })
        filename = f"Handover_{handover.shift.name}_{handover.date}.pdf"
        pdf['Content-Disposition'] = f'attachment; filename="{filename}"'
        return pdf
    except ShiftHandover.DoesNotExist:
        return HttpResponse("Handover not found", status=404)
    except PermissionDenied:
        return HttpResponse("Forbidden", status=403)
