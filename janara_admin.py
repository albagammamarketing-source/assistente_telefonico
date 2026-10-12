"""Janara private read-only admin chat, designed for existing FastAPI app on Render.
No mutating Sheets/Calendar/Gmail calls. Password-protected HTTP Basic session.
"""
import os, re, hmac, html, calendar
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from zoneinfo import ZoneInfo
from typing import Optional
from urllib.parse import quote

import requests
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from icalendar import Calendar

router = APIRouter(prefix='/admin-janara', tags=['Janara admin'])
security = HTTPBasic()
TZ = ZoneInfo('Europe/Rome')
SPREADSHEET = os.getenv('JANARA_REGISTRO_SHEET_ID', '1DzP9o98cV6w0_wz581E8MgWt7iOFOr9gJ2wuVaZPV98')
REG_TAB = os.getenv('JANARA_REGISTRO_TAB', 'check IN JAnara')
CONFIG_SPREADSHEET = os.getenv('SPREADSHEET_ID', '1orw2D-Rxh2omVj_MOBIdIsiLf90UjsvSZrpQqKYeTys')
CREDENTIALS = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '/etc/secrets/google_calendar_credentials.json')
HTTP_TIMEOUT = 12


def auth(c: HTTPBasicCredentials = Depends(security)):
    username = os.getenv('JANARA_ADMIN_USER', '')
    password = os.getenv('JANARA_ADMIN_PASSWORD', '')
    if not username or not password or not (hmac.compare_digest(c.username, username) and hmac.compare_digest(c.password, password)):
        raise HTTPException(status_code=401, detail='Credenziali non valide', headers={'WWW-Authenticate': 'Basic realm="Janara"'})
    return c.username


def sheets():
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def values(sheet_id, tab, rng):
    escaped = tab.replace("'", "''")
    return sheets().spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{escaped}'!{rng}").execute().get('values', [])


def cell(row, idx):
    return str(row[idx]).strip() if idx < len(row) and row[idx] is not None else ''


def amount(s):
    s = str(s or '').strip().replace('€', '').replace(' ', '')
    if not s: return None
    # Italian-style decimal separator, including thousands separators
    if ',' in s: s = s.replace('.', '').replace(',', '.')
    try: return Decimal(s)
    except InvalidOperation: return None


def dt(s):
    v = str(s or '').strip().lower()
    for fmt in ('%d/%m/%Y','%Y-%m-%d','%d-%b-%Y','%d/%m/%y','%d-%m-%Y'):
        try: return datetime.strptime(v,fmt).date()
        except ValueError: pass
    return None


def norm(s):
    s = str(s or '').lower().replace('’', "'").replace('_', ' ')
    return re.sub(r'[^a-z0-9]+', '', s)

# Explicit aliases, never silently infer two unrelated rooms match.
ALIASES = {
    'monolocaleduomo': 'duomo', 'duomo': 'duomo',
    'portarsa': 'port_arsa', 'santasofa':'santa_sofia', 'santasofia':'santa_sofia',
    'muralongobarde':'mura_longobarde', 'muralongobarde':'mura_longobarde',
    'arcoditraiano':'arco_di_traiano', 'roccadeirettori':'rocca_dei_rettori',
    'teatroromano':'teatro_romano', 'arechi':'arechi', 'iside':'iside',
}


def room_key(text):
    n=norm(text)
    return ALIASES.get(n, n)


def read_registry():
    raw = values(SPREADSHEET, REG_TAB, 'A1:AO5101')
    out=[]
    for index, row in enumerate(raw[1:], 2):
        name=cell(row,3)
        start=dt(cell(row,5)); end=dt(cell(row,8))
        if not (name and start and end and end>start): continue
        out.append({'row':index, 'name':name, 'room':cell(row,2), 'room_key':room_key(cell(row,2)),
                    'arrival':start, 'departure':end, 'paid_guest':amount(cell(row,14)),
                    'cash_due':amount(cell(row,15)), 'gross':amount(cell(row,23)),
                    'note':cell(row,13), 'guests':cell(row,4)})
    return out


def rooms():
    raw=values(CONFIG_SPREADSHEET,'CAMERE_CONFIG','A1:N60')
    if not raw: raise RuntimeError('CAMERE_CONFIG vuoto')
    header=[str(k).strip() for k in raw[0]]
    data=[]
    for row in raw[1:]:
        r=dict(zip(header,row))
        if str(r.get('attiva','')).strip().upper()=='SI': data.append(r)
    return data


def booked(url):
    resp=requests.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    cal=Calendar.from_ical(resp.content)
    result=[]
    for ev in cal.walk('VEVENT'):
        st=ev.get('DTSTART'); en=ev.get('DTEND')
        if not st or not en: continue
        a=st.dt; b=en.dt
        if isinstance(a,datetime): a=a.astimezone(TZ).date() if a.tzinfo else a.date()
        if isinstance(b,datetime): b=b.astimezone(TZ).date() if b.tzinfo else b.date()
        if b>a: result.append((a,b))
    return result


def requested_date(q):
    today=datetime.now(TZ).date()
    iso=re.search(r'\b(20\d{2}-\d{2}-\d{2})\b',q)
    if iso: return dt(iso.group(1))
    dmy=re.search(r'\b(\d{1,2}/\d{1,2}/20\d{2})\b',q)
    if dmy: return dt(dmy.group(1))
    low=q.lower()
    if 'dopodomani' in low: return today+timedelta(days=2)
    if 'domani' in low: return today+timedelta(days=1)
    return today


def fmt_money(x):
    return 'non indicato' if x is None else f'€{x:,.2f}'.replace(',','X').replace('.',',').replace('X','.')


def bformat(b):
    return f"{b['name']} — {b['room'] or 'CAMERA NON COMPILATA'} — {b['arrival']:%d/%m} → {b['departure']:%d/%m}; cash {fmt_money(b['cash_due'])}; pagato ospite {fmt_money(b['paid_guest'])} (riga {b['row']})"


def answer(q):
    when=requested_date(q)
    query=q.lower().strip()
    data=read_registry()
    today=datetime.now(TZ).date()
    if any(w in query for w in ('arriv', 'check-in','check in')):
        matches=[b for b in data if b['arrival']==when]
        return {'text':f"Arrivi del {when:%d/%m/%Y}: {len(matches)}.\n" + ('\n'.join('• '+bformat(b) for b in matches) if matches else 'Nessun arrivo nel registro.'), 'source':'Google Sheet registro'}
    if any(w in query for w in ('partenz','check-out','check out')):
        matches=[b for b in data if b['departure']==when]
        return {'text':f"Partenze del {when:%d/%m/%Y}: {len(matches)}.\n" + ('\n'.join('• '+bformat(b) for b in matches) if matches else 'Nessuna partenza nel registro.'),'source':'Google Sheet registro'}
    if any(w in query for w in ('cash','contant','incassare','riscuotere')):
        matches=[b for b in data if b['arrival']==when and b['cash_due'] is not None and b['cash_due']>0]
        total=sum((b['cash_due'] for b in matches),Decimal('0'))
        return {'text':f"Contanti indicati in colonna P per gli arrivi del {when:%d/%m/%Y}: {fmt_money(total)}.\n" + ('\n'.join('• '+bformat(b) for b in matches) if matches else 'Nessun saldo cash positivo indicato.'),'source':'Google Sheet colonna P'}
    if any(w in query for w in ('liber','disponibil','occupat','ical')):
        rlist=rooms(); lines=[]; ok=0; errors=0
        for r in rlist:
            name=str(r.get('nome_camera',''))
            url=str(r.get('ical_url','')).strip()
            if not url: lines.append(f'• {name}: NON VERIFICABILE (iCal assente)'); errors+=1; continue
            try:
                ranges=booked(url)
                taken=any(a<=when<b for a,b in ranges)
                lines.append(f"• {name}: {'OCCUPATA/BLOCCATA' if taken else 'LIBERA secondo iCal'}")
                if not taken: ok+=1
            except Exception:
                lines.append(f'• {name}: NON VERIFICABILE (feed non accessibile)'); errors+=1
        return {'text':f"Situazione per la notte del {when:%d/%m/%Y}: {ok} camere senza blocco iCal rilevato, {errors} calendari non verificabili.\n" + '\n'.join(lines)+"\nNota: iCal indica disponibilità/blocchi, non identifica necessariamente l'ospite. Prima di confermare una vendita serve controllo aggiornato Beddy.",'source':'iCal Beddy da CAMERE_CONFIG'}
    if any(w in query for w in ('anomali','mancant','differenz','verific')):
        matches=[b for b in data if today-timedelta(days=3)<=b['arrival']<=today+timedelta(days=45) and not b['room']]
        return {'text':f"Prenotazioni senza camera nel registro (arrivi tra {today-timedelta(days=3):%d/%m/%Y} e {today+timedelta(days=45):%d/%m/%Y}): {len(matches)}.\n" + '\n'.join('• '+bformat(b) for b in matches[:30]) + "\nPer confrontare anche le conferme email Beddy occorre integrare un accesso Gmail server-side in sola lettura.",'source':'Google Sheet registro; controllo parziale'}
    return {'text':"Posso rispondere ora a: 'chi arriva oggi?', 'chi parte domani?', 'quanto cash devo riscuotere oggi?', 'quali camere sono libere il 2026-10-20?' e 'quali prenotazioni sono senza camera?'. La lettura Gmail e la riconciliazione degli accrediti OTA sono ancora da integrare.", 'source':'Funzioni disponibili nella versione iniziale'}


class ChatRequest(BaseModel):
    message: str


HTML = r'''<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Janara AI · Reception</title>
<style>
body{margin:0;font:16px system-ui,sans-serif;background:#f3f5f8;color:#17212d}main{max-width:790px;margin:0 auto;padding:20px}h1{margin:0 0 4px}.hint{color:#667586;font-size:13px}.chat{margin:18px 0;min-height:220px;background:white;border-radius:16px;padding:20px;box-shadow:0 4px 20px #0001;white-space:pre-wrap}.m{padding:12px 15px;margin:10px 0;border-radius:12px;background:#eaf1ff;overflow-wrap:anywhere}.a{background:#f2f5f8}form{display:flex;gap:8px;flex-wrap:wrap}input{flex:1;min-width:150px;padding:14px;border:1px solid #ccd3de;border-radius:10px;font:inherit}button{background:#173a62;color:white;border:0;border-radius:10px;padding:12px 17px;font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}button.secondary{background:#dde6f0;color:#173a62}.mic.on{background:#b32937}small{color:#6e7d8c}.tools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:12px}.status{font-size:13px;color:#526478}label{font-size:14px}input[type=checkbox]{min-width:0;vertical-align:middle}button:focus-visible,input:focus-visible{outline:3px solid #5f95d6;outline-offset:2px}
</style></head><body><main>
<h1>Janara AI · Reception</h1><div class="hint">Chat gestionale privata · fonti in sola lettura</div>
<div id="chat" class="chat" role="log" aria-live="polite"><div class="a m">Benvenuto. Chiedimi chi arriva oggi, quanto cash va riscosso o quali camere sono libere in una data.</div></div>
<form id="f"><input id="q" aria-label="Domanda" placeholder="Scrivi una domanda oppure usa il microfono..." maxlength="1000" autocomplete="off" required><button id="send" type="submit">Invia</button><button id="mic" type="button" class="secondary mic" aria-pressed="false" aria-label="Avvia dettatura">🎤 Parla</button></form>
<div class="tools"><label><input id="speak" type="checkbox"> Leggi le risposte ad alta voce</label><button id="stopvoice" type="button" class="secondary">■ Ferma voce</button><span id="status" class="status" role="status"></span></div>
<p><small>Per usare il microfono, autorizza l'accesso quando richiesto dal browser. Il riconoscimento vocale dipende dal browser e potrebbe usare un servizio esterno. Evita l'ascolto delle risposte con dati degli ospiti in luoghi pubblici. Nessuna modifica automatica alle prenotazioni.</small></p>
</main><script>
'use strict';
const box=document.querySelector('#chat');
const form=document.querySelector('#f');
const inp=document.querySelector('#q');
const sendBtn=document.querySelector('#send');
const micBtn=document.querySelector('#mic');
const speakBox=document.querySelector('#speak');
const stopBtn=document.querySelector('#stopvoice');
const status=document.querySelector('#status');
let busy=false, listening=false, rec=null;
function add(t,k){const d=document.createElement('div');d.className='m '+k;d.textContent=t;box.appendChild(d);d.scrollIntoView({block:'end',behavior:'smooth'});}
function setStatus(t){status.textContent=t;}
function stopSpeaking(){if('speechSynthesis' in window) window.speechSynthesis.cancel();}
function speak(t){if(!speakBox.checked||!('speechSynthesis' in window))return;stopSpeaking();const u=new SpeechSynthesisUtterance(t);u.lang='it-IT';u.rate=1;window.speechSynthesis.speak(u);}
async function sendQuestion(q){q=(q||'').trim();if(!q||busy)return;busy=true;sendBtn.disabled=true;micBtn.disabled=true;inp.value='';stopSpeaking();add(q,'m');setStatus('Sto cercando le informazioni...');try{const r=await fetch('/admin-janara/ask',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({message:q})});if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();const answer=d.answer||'Nessuna risposta disponibile';add(answer+'\n\nFonte: '+(d.source||'non indicata'),'a');setStatus('Risposta ricevuta');speak(answer);}catch(e){add('Impossibile recuperare i dati: '+e.message,'a');setStatus('Errore nel recupero dei dati');}finally{busy=false;sendBtn.disabled=false;micBtn.disabled=false;}}
form.addEventListener('submit',e=>{e.preventDefault();sendQuestion(inp.value);});
const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
if(!Recognition){micBtn.disabled=true;micBtn.title='Riconoscimento vocale non supportato da questo browser';setStatus('Microfono non supportato: prova Chrome o Edge aggiornati, oppure scrivi.');}
else{
 rec=new Recognition();rec.lang='it-IT';rec.continuous=false;rec.interimResults=false;rec.maxAlternatives=1;
 rec.onstart=()=>{listening=true;micBtn.classList.add('on');micBtn.setAttribute('aria-pressed','true');micBtn.textContent='■ Stop';setStatus('Ti ascolto...');};
 rec.onresult=e=>{const t=e.results?.[0]?.[0]?.transcript?.trim()||'';if(t){inp.value=t;setStatus('Domanda riconosciuta');}};
 rec.onerror=e=>{setStatus(e.error==='not-allowed'?'Accesso al microfono negato: abilitalo nelle impostazioni del browser.':'Microfono: '+e.error);};
 rec.onend=()=>{listening=false;micBtn.classList.remove('on');micBtn.setAttribute('aria-pressed','false');micBtn.textContent='🎤 Parla';if(inp.value.trim()&&!busy){const q=inp.value.trim();sendQuestion(q);}};
 micBtn.addEventListener('click',()=>{if(listening){rec.stop();return;}if(busy)return;inp.value='';stopSpeaking();try{rec.start();}catch(e){setStatus('Impossibile avviare il microfono: '+e.message);}});
}
stopBtn.addEventListener('click',stopSpeaking);
speakBox.addEventListener('change',()=>{if(!speakBox.checked)stopSpeaking();});
</script></body></html>'''

@router.get('/', response_class=HTMLResponse)
def index(_=Depends(auth)):
    return HTML


@router.post('/ask')
def ask(body:ChatRequest,_=Depends(auth)):
    if len(body.message)>1000: raise HTTPException(400,'Domanda troppo lunga')
    try:
        a=answer(body.message)
    except Exception as e:
        # Do not leak tokens, filesystem paths or service credentials
        raise HTTPException(503,'Fonti temporaneamente non accessibili; verificare permessi e collegamenti') from None
    return {'answer':a['text'],'source':a['source'],'read_only':True}
