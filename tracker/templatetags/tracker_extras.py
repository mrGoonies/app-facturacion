from django import template

register = template.Library()


@register.filter
def hm(duration):
    """Formats a timedelta as '3h 41m', matching the mockup's duration style."""
    if duration is None:
        return "—"
    total_minutes = int(duration.total_seconds() // 60)
    if total_minutes < 0:
        return "—"
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours:02d} h"
    return f"{hours} h {minutes:02d} m"


@register.filter
def hours_only(duration):
    if duration is None:
        return "—"
    return f"{duration.total_seconds() / 3600:.0f} h"
