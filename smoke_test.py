from app.core.config import seatsio_enabled, target_blocks, use_stealth_browser
from app.services.seatsio_token_fetcher import CACHE
from app.services.seatsio_client import pick_adjacent_from_snapshot
from app.services.booking_http import resolve_seated_manifest, prewarm_event_from_slug

assert isinstance(seatsio_enabled(), bool)
assert isinstance(target_blocks(), list)
assert isinstance(use_stealth_browser(), bool)
assert hasattr(CACHE, 'to_dict')

rendering = {
    'objects': [
        {'id': 'A-1', 'labels': {'section': 'S1', 'parent': 'A', 'own': '1', 'displayedLabel': 'A-1'}},
        {'id': 'A-2', 'labels': {'section': 'S1', 'parent': 'A', 'own': '2', 'displayedLabel': 'A-2'}},
        {'id': 'A-3', 'labels': {'section': 'S1', 'parent': 'A', 'own': '3', 'displayedLabel': 'A-3'}},
    ]
}
statuses = {'A-1': 'free', 'A-2': 'free', 'A-3': 'booked'}
chosen = pick_adjacent_from_snapshot(rendering, statuses, 2, target_blocks=['S1'])
assert chosen == ['A-1', 'A-2']
print('SMOKE_OK')
