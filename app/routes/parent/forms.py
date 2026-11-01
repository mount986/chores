"""Parsers for form fields shared by several parent routes."""
from flask import request


def parse_unit_form(flag_field='is_per_unit', label_field='unit_label'):
    """Read the per-unit toggle and unit label from the submitted form, as Chore kwargs."""
    is_per_unit = request.form.get(flag_field) == 'on'
    label = request.form.get(label_field, '').strip()[:30] if is_per_unit else ''
    return {'is_per_unit': is_per_unit, 'unit_label': label or None}


def parse_recurrence_form():
    """Read is_recurring / recurrence_cadence / recurrence_day from the submitted form."""
    is_recurring = request.form.get('is_recurring') == 'on'
    cadence = request.form.get('recurrence_cadence', '').strip() if is_recurring else None
    if cadence not in ('daily', 'weekly', 'monthly'):
        return False, None, None
    day = None
    if cadence in ('weekly', 'monthly'):
        default_day = 0 if cadence == 'weekly' else 1
        try:
            day = int(request.form.get('recurrence_day', default_day))
        except (ValueError, TypeError):
            day = default_day
    return True, cadence, day
