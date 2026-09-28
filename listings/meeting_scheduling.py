from django.db.models import Q

from .models import MeetingAppointment


def find_appointment_conflicts(*, user, start_at, end_at, exclude_order_id=None, lock=False):
    """Return active delivery appointments that overlap a candidate time window."""
    appointments = MeetingAppointment.objects.filter(
        status__in={'pending', 'confirmed'},
        start_at__lt=end_at,
        end_at__gt=start_at,
    ).filter(
        Q(order__buyer=user) | Q(order__seller=user),
    ).select_related('order__item', 'order__buyer', 'order__seller')
    if exclude_order_id is not None:
        appointments = appointments.exclude(order_id=exclude_order_id)
    appointments = appointments.order_by('start_at', 'id')
    return appointments.select_for_update() if lock else appointments
