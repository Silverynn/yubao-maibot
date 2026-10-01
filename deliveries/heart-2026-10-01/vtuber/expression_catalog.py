"""Read expressions declared by the currently selected Open-LLM-VTuber model.

The model URL must stay inside this VTuber installation's live2d-models dir.
No remote fetches and no arbitrary file reads are needed for /表情列表.
"""

import json
from pathlib import Path


def model_expression_names(model):
    info = getattr(model, 'model_info', None) or {}
    url = info.get('url', '')
    if not isinstance(url, str) or not url.startswith('/live2d-models/'):
        return ()
    models_root = (Path(__file__).resolve().parent / 'live2d-models').resolve()
    path = (Path(__file__).resolve().parent / url.lstrip('/')).resolve()
    if not path.is_relative_to(models_root) or not path.is_file():
        return ()
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))['FileReferences']['Expressions']
        return tuple(dict.fromkeys(entry['Name'] for entry in entries
                                   if isinstance(entry, dict) and isinstance(entry.get('Name'), str)))
    except (OSError, ValueError, KeyError, TypeError):
        return ()
