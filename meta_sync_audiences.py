#!/usr/bin/env python3
"""
meta_sync_audiences.py — sincroniza Custom Audiences de Meta desde el Sheet CRM `PT Sin vuelos`.

Source of truth: Google Sheet 1IxTWD5RNc9m3LitTtkBKbKm72a-jR7yFNmrMop40vy4, hoja "PT Sin vuelos".
- Estado = valor más a la derecha no-vacío entre cols M/N/O.
- Fila vacía en M/N/O pero con email/phone → tratar como "Contactado" (lead reciente sin atender).

Mapping de estados a audiencias Meta:
- Comprado, Comprado 2025           → Clientes PT (value=2000, Value-Based Custom Audience seed del LAL)
- Caliente, Comprará, Llamada, Curso,
  Contactado, Pdt reserva, 2027     → CRM-Retargeting_Mid (Customer List)
- 2027                              → Pool_2027 (duplicado, parkeada Q4)
- No responde, No cumple, NA,
  Descartado, Error                 → CRM-Excluir ↓
- Todo el universo (cualquier lead) → CRM-Excluir (añadir además)

Uso:
    python3 meta_sync_audiences.py --dry-run        # muestra diff, no aplica
    python3 meta_sync_audiences.py --apply          # aplica los cambios
    python3 meta_sync_audiences.py --apply --force  # ignora guard >50 delta (pedir confirmación)

Safety:
- Dry-run por defecto de primera ejecución sin cache previa.
- Guard: si delta (|add|+|remove|) >50 en CUALQUIER audiencia → abort (salvo --force).
- Cache por audiencia: ~/padel-content-generator/output/meta_sync_cache/<audience>.json
- Log rotación en ~/padel-content-generator/logs/meta_sync.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Google Sheets
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Meta
sys.path.insert(0, str(Path(__file__).parent))
from meta_client import init_api
from facebook_business.adobjects.customaudience import CustomAudience

# ============ CONFIG ============
SHEET_ID = '1IxTWD5RNc9m3LitTtkBKbKm72a-jR7yFNmrMop40vy4'
SHEET_NAME = 'PT Sin vuelos'
SHEET_RANGE = f'{SHEET_NAME}!A1:O2000'

AUDIENCES = {
    'clientes_pt': {
        'id': '120242531887780028',
        'name': 'Clientes PT',
        'states': {'Comprado', 'Comprado 2025'},
        'value_based': True,
        'lookalike_value': 2000,
    },
    'retargeting_mid': {
        'id': '120245031106360028',
        'name': 'CRM-Retargeting_Mid',
        'states': {'Caliente', 'Comprará', 'Llamada', 'Curso', 'Contactado', 'Pdt reserva', '2027'},
        'value_based': False,
    },
    # Pool_2027 deshabilitado por decisión 2026-04-22 (usuario: "pool 2027 no la crees").
    # Los pax con estado '2027' se incluyen en CRM-Retargeting_Mid.
    'excluir': {
        'id': '120245032496340028',
        'name': 'CRM-Excluir',
        'states': {'No responde', 'No cumple', 'NA', 'Descartado', 'Error'},
        'value_based': False,
        'include_universe': True,  # añade TODO el universo del Sheet (leads + clientes) a Excluir
    },
}

# Estados conocidos (para validar parsing y detectar typos)
KNOWN_STATES = {
    'Comprado', 'Comprado 2025', 'Pdt reserva', 'Comprará', 'Llamada', 'Caliente',
    'Curso', 'Contactado', '2027', 'No responde', 'No cumple', 'NA', 'Error', 'Descartado',
}

EMPTY_STATE_FALLBACK = 'Contactado'  # filas con email/phone pero M/N/O vacíos → Contactado

DELTA_WARN_THRESHOLD = 100  # delta (add+remove) por audiencia que dispara abort (subido 2026-04-30 tras CRM activo diario)

REPO_DIR = Path(__file__).parent
CACHE_DIR = REPO_DIR / 'output' / 'meta_sync_cache'
LOG_DIR = REPO_DIR / 'logs'
CREDS_FILE = REPO_DIR / 'credentials.json'
TOKEN_FILE = REPO_DIR / 'token.json'

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# El token.json fue autorizado solo con scope 'drive' (suficiente para leer Sheets vía la Sheets API).
# Pedir además 'spreadsheets.readonly' rompía el refresh con invalid_scope (scope no concedido al refresh token).
SCOPES = ['https://www.googleapis.com/auth/drive']


# ============ LOG ============
def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG_DIR / 'meta_sync.log', 'a') as f:
        f.write(line + '\n')


# ============ NORMALIZE ============
def normalize_email(e):
    if not e: return ''
    return str(e).lower().strip()


def normalize_phone(p):
    """Normalize phone for hashing.
    - Strip non-digits
    - Strip Spanish country code (0034, 34) for ES numbers only
    - Keep other country codes as-is: +376 (Andorra), +52 (México), +36 (Hungría), +350 (Gib), +63 (Philippines), etc.
      — estos se hashean con su prefijo intl, que es como están registrados en Meta.
    """
    if p is None or p == '': return ''
    s = str(p)
    if s.endswith('.0'): s = s[:-2]
    d = re.sub(r'\D', '', s)
    if d.startswith('0034'): d = d[4:]
    if d.startswith('34') and len(d) == 11: d = d[2:]
    return d


def sha256_hex(v: str) -> str:
    return hashlib.sha256(v.encode('utf-8')).hexdigest()


def make_user_key(email_norm: str, phone_norm: str) -> str:
    """Stable unique key per person. Prefer email if present, fallback to phone."""
    return f'e:{email_norm}' if email_norm else f'p:{phone_norm}'


# ============ GOOGLE SHEETS ============
def get_sheets_service():
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build('sheets', 'v4', credentials=creds)


def load_sheet_rows():
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=SHEET_RANGE
    ).execute()
    values = result.get('values', [])
    if not values:
        raise RuntimeError('Sheet vacío')
    header = values[0]
    log(f'Sheet loaded: {len(values)-1} data rows · header: {header}')
    # Indices (0-based): I=8 Nombre, J=9 Teléfono, K=10 Mail, M=12, N=13, O=14
    rows = []
    unknowns = []
    empty_with_contact = 0
    for i, row in enumerate(values[1:], start=2):
        # Pad to 15 cols
        r = row + [''] * (15 - len(row))
        nombre = (r[8] or '').strip()
        phone_raw = r[9] if len(r) > 9 else ''
        email_raw = r[10] if len(r) > 10 else ''
        M = (r[12] or '').strip()
        N = (r[13] or '').strip()
        O = (r[14] or '').strip()
        email_n = normalize_email(email_raw)
        phone_n = normalize_phone(phone_raw)
        # Skip rows with no contact info at all
        if not email_n and not phone_n:
            continue
        # Determine state
        current = ''
        for s in [O, N, M]:
            if s in KNOWN_STATES:
                current = s; break
        if not current:
            # Has contact but no known state
            if M or N or O:
                unknowns.append({'row': i, 'M': M, 'N': N, 'O': O, 'name': nombre})
                current = EMPTY_STATE_FALLBACK
            else:
                # all M/N/O empty
                current = EMPTY_STATE_FALLBACK
                empty_with_contact += 1
        rows.append({
            'row': i, 'name': nombre,
            'email': email_n, 'phone': phone_n,
            'email_raw': email_raw, 'phone_raw': phone_raw,
            'M': M, 'N': N, 'O': O, 'state': current,
            'key': make_user_key(email_n, phone_n),
        })
    log(f'Rows con contact: {len(rows)} · empty→Contactado: {empty_with_contact} · unknown states: {len(unknowns)}')
    if unknowns:
        log(f'  unknown states (first 5): {unknowns[:5]}')
    # Dedup by key: prefer the "highest priority" state (Comprado > Hot > Mid > Excluir)
    # For now, keep last occurrence per key (sheet order assumed chronological)
    by_key = {}
    for r in rows:
        by_key[r['key']] = r
    log(f'Unique people (dedup by email/phone): {len(by_key)}')
    return list(by_key.values())


# ============ AUDIENCE TARGETS ============
def compute_targets(rows):
    """For each audience, compute the target set of users (dicts with email+phone hashed)."""
    targets = {}
    for key, cfg in AUDIENCES.items():
        members = []
        seen = set()
        for r in rows:
            if r['state'] in cfg['states']:
                if r['key'] in seen: continue
                seen.add(r['key'])
                members.append(r)
        if cfg.get('include_universe'):
            # Excluir: también todos los leads/clientes (cualquier estado conocido)
            for r in rows:
                if r['key'] in seen: continue
                seen.add(r['key'])
                members.append(r)
        targets[key] = members
        log(f'  target {cfg["name"]}: {len(members)} pax')
    return targets


# ============ META AUDIENCE OPS ============
def build_payload(users, include_value=None):
    """Build Meta API payload pre-hashed. Schema = [EMAIL, PHONE] (+ LOOKALIKE_VALUE if value-based)."""
    schema = ['EMAIL', 'PHONE']
    if include_value is not None:
        schema.append('LOOKALIKE_VALUE')
    data = []
    for u in users:
        row = [
            sha256_hex(u['email']) if u['email'] else '',
            sha256_hex(u['phone']) if u['phone'] else '',
        ]
        if include_value is not None:
            row.append(str(include_value))
        data.append(row)
    return schema, data


def apply_to_audience(audience_id, op, users, include_value=None, batch_size=1000):
    """op: 'add', 'remove', 'replace'. Returns batches submitted."""
    ca = CustomAudience(audience_id)
    schema, data = build_payload(users, include_value=include_value)
    total = len(data)
    if op == 'replace':
        # users_replace requires top-level `session` param (sibling of `payload`).
        session_id = int(time.time() * 1000)
        est_num_total = total
        for i in range(0, total, batch_size):
            chunk = data[i:i+batch_size]
            is_last = (i + batch_size) >= total
            payload = {'schema': schema, 'data': chunk}
            session = {
                'session_id': session_id,
                'batch_seq': (i // batch_size) + 1,
                'last_batch_flag': is_last,
                'estimated_num_total': est_num_total,
            }
            ca.create_users_replace(params={'payload': payload, 'session': session})
            log(f'    replace batch {(i // batch_size) + 1}: {min(i+batch_size, total)}/{total} last={is_last}')
            time.sleep(0.3)
        return
    for i in range(0, total, batch_size):
        chunk = data[i:i+batch_size]
        payload = {'schema': schema, 'data': chunk}
        if op == 'add':
            ca.create_user(params={'payload': payload})
        elif op == 'remove':
            ca.delete_users(params={'payload': payload})
        log(f'    {op} {min(i+batch_size, total)}/{total}')
        time.sleep(0.3)


def get_audience_count(audience_id):
    ca = CustomAudience(audience_id).api_get(
        fields=['approximate_count_lower_bound', 'approximate_count_upper_bound']
    )
    return ca.get('approximate_count_lower_bound'), ca.get('approximate_count_upper_bound')


def find_audience_by_name(name):
    """Busca audience_id by name via api (para Pool_2027 que no tengo ID)."""
    from facebook_business.adobjects.adaccount import AdAccount
    acc = AdAccount('act_1184362669591396')
    for a in acc.get_custom_audiences(fields=['id', 'name']):
        if a.get('name') == name:
            return a['id']
    return None


# ============ SYNC ============
def load_cache(aud_key):
    p = CACHE_DIR / f'{aud_key}.json'
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_cache(aud_key, users):
    p = CACHE_DIR / f'{aud_key}.json'
    p.write_text(json.dumps([{'key': u['key'], 'email': u['email'], 'phone': u['phone']} for u in users], ensure_ascii=False, indent=2))


def sync_audience(aud_key, cfg, target_users, apply_changes, force, reset=False):
    log(f'\n=== {cfg["name"]} ({aud_key}) ===')
    if not cfg['id']:
        found_id = find_audience_by_name(cfg['name'])
        if not found_id:
            log(f'  ⚠️  No existe audience "{cfg["name"]}" en Meta. Skip.')
            return {'skipped': True}
        cfg['id'] = found_id
        log(f'  Resolved id: {found_id}')

    target_keys = {u['key'] for u in target_users}

    if reset:
        log(f'  🔄 RESET mode: users_replace con {len(target_users)} users')
        if apply_changes:
            val = cfg.get('lookalike_value') if cfg.get('value_based') else None
            apply_to_audience(cfg['id'], 'replace', target_users, include_value=val)
            save_cache(aud_key, target_users)
            log(f'  ✅ Reset ok · cache actualizada con {len(target_users)} users')
        return {'reset': True, 'target': len(target_keys)}

    # Incremental sync (has cache)
    cache = load_cache(aud_key)
    current_keys = {u['key'] for u in cache} if cache else set()
    target_by_key = {u['key']: u for u in target_users}

    if cache is None:
        log(f'  ⚠️  No cache previa — usa --reset para sync inicial (users_replace).')
        return {'no_cache': True, 'target': len(target_keys)}

    to_add_keys = target_keys - current_keys
    to_remove_keys = current_keys - target_keys
    to_add = [target_by_key[k] for k in to_add_keys]
    cache_by_key = {u['key']: u for u in cache}
    to_remove = [cache_by_key[k] for k in to_remove_keys if k in cache_by_key]

    log(f'  target={len(target_keys)}  cached={len(current_keys)}  +{len(to_add)} / -{len(to_remove)}')

    delta = len(to_add) + len(to_remove)
    if delta > DELTA_WARN_THRESHOLD and not force:
        log(f'  🚨 DELTA {delta} > {DELTA_WARN_THRESHOLD} — abort (usa --force para ignorar)')
        return {'aborted': True, 'delta': delta, 'to_add': len(to_add), 'to_remove': len(to_remove)}

    if apply_changes:
        if to_remove:
            log(f'  Removing {len(to_remove)}...')
            apply_to_audience(cfg['id'], 'remove', to_remove)
        if to_add:
            val = cfg.get('lookalike_value') if cfg.get('value_based') else None
            log(f'  Adding {len(to_add)}' + (f' (value={val})' if val else '') + '...')
            apply_to_audience(cfg['id'], 'add', to_add, include_value=val)
        save_cache(aud_key, target_users)
        log(f'  ✅ Cache actualizada con {len(target_users)} users')
    else:
        for u in to_add[:3]:
            log(f'    + {u["name"]}  email={u["email"] or "-"}  phone={u["phone"] or "-"}')
        for u in to_remove[:3]:
            log(f'    - {u.get("email","") or u.get("phone","")}')

    return {
        'target': len(target_keys),
        'cached': len(current_keys),
        'to_add': len(to_add),
        'to_remove': len(to_remove),
    }


# ============ MAIN ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='Aplicar cambios (default: dry-run)')
    ap.add_argument('--force', action='store_true', help='Ignorar safety delta >50')
    ap.add_argument('--reset', action='store_true', help='Reset inicial: users_replace atómico contra target (para primera ejecución)')
    args = ap.parse_args()

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    if args.reset: mode += ' +RESET'
    log(f'═══════ meta_sync_audiences.py — {mode} — {datetime.now().isoformat()} ═══════')

    init_api()
    rows = load_sheet_rows()
    targets = compute_targets(rows)

    summary = {}
    for aud_key, cfg in AUDIENCES.items():
        summary[aud_key] = sync_audience(aud_key, cfg, targets[aud_key], args.apply, args.force, reset=args.reset)

    log('\n═══════ SUMMARY ═══════')
    for k, r in summary.items():
        log(f'  {AUDIENCES[k]["name"]:25s} → {r}')

    # Step encadenado: sync de outcomes WhatsApp bot (D1)
    # Reusa el mismo Sheet load → evita 2ª llamada API.
    try:
        log('\n═══════ WhatsApp bot sync ═══════')
        from whatsapp_sync_outcomes import load_sheet_outcomes, post_to_worker
        payload = load_sheet_outcomes()
        if payload['sold_phones'] or payload['lost_phones']:
            result = post_to_worker(payload)
            if result:
                log(f'  ✓ WhatsApp D1 sync OK: {result}')
            else:
                log(f'  ❌ WhatsApp D1 sync FAILED')
        else:
            log('  Nada que sincronizar en WhatsApp.')
    except Exception as e:
        log(f'  ⚠ whatsapp_sync_outcomes error (no bloqueante): {e}')


if __name__ == '__main__':
    main()
