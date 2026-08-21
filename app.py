import hashlib, math, os, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DB_PATH = os.getenv('DB_PATH', str(Path(__file__).with_name('taxi_ya.sqlite3')))
ACTIVATION_CODE = os.getenv('TAXI_ACTIVATION_CODE', '123456')
app = FastAPI(title='Taxi Ya API', version='0.1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

SCHEMA = '''
CREATE TABLE IF NOT EXISTS drivers (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, license TEXT, plate TEXT, status TEXT NOT NULL DEFAULT 'pending', device_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stops (id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, latitude REAL, longitude REAL, radius_m REAL NOT NULL DEFAULT 50, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue_entries (id TEXT PRIMARY KEY, stop_id TEXT NOT NULL, driver_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', joined_at TEXT NOT NULL, left_at TEXT, UNIQUE(stop_id, driver_id), FOREIGN KEY(stop_id) REFERENCES stops(id), FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS stop_presence (driver_id TEXT NOT NULL, stop_id TEXT NOT NULL, entered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, inside INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(driver_id, stop_id), FOREIGN KEY(stop_id) REFERENCES stops(id), FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS rides (id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, origin_lat REAL, origin_lng REAL, destination_lat REAL, destination_lng REAL, driver_id TEXT, status TEXT NOT NULL DEFAULT 'requested', eta_minutes INTEGER, estimated_price REAL, created_at TEXT NOT NULL, accepted_at TEXT, FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS locations (entity_id TEXT NOT NULL, role TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, ride_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(entity_id, role));
'''

def now(): return datetime.now(timezone.utc).isoformat()
def db():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row; con.execute('PRAGMA foreign_keys=ON'); return con
def init_db():
    con=db(); con.executescript(SCHEMA)
    cols={r['name'] for r in con.execute('PRAGMA table_info(stops)').fetchall()}
    for name,definition in [('latitude','REAL'),('longitude','REAL'),('radius_m','REAL NOT NULL DEFAULT 50')]:
        if name not in cols: con.execute(f'ALTER TABLE stops ADD COLUMN {name} {definition}')
    con.execute('UPDATE stops SET radius_m=50 WHERE radius_m IS NULL')
    con.commit(); con.close()

def row_dict(row): return dict(row) if row else None
def code_hash(code): return hashlib.sha256(code.encode()).hexdigest()

def haversine(a_lat,a_lng,b_lat,b_lng):
    if None in (a_lat,a_lng,b_lat,b_lng): return None
    r=6371; p=math.pi/180; dlat=(b_lat-a_lat)*p; dlng=(b_lng-a_lng)*p
    x=math.sin(dlat/2)**2+math.cos(a_lat*p)*math.cos(b_lat*p)*math.sin(dlng/2)**2
    return r*2*math.asin(math.sqrt(x))

def estimate(ride):
    km=haversine(ride.get('origin_lat'),ride.get('origin_lng'),ride.get('destination_lat'),ride.get('destination_lng'))
    km = km if km is not None else 4.0
    eta=max(3, math.ceil(km/0.45))
    price=4.20 + km*1.05 + eta*0.18
    return eta, round(price,2)

class DriverRegister(BaseModel):
    name: str = Field(min_length=2); phone: str = Field(min_length=6); license: str=''; plate: str=''
class DriverActivate(BaseModel):
    driver_id: str; code: str; device_id: str = Field(min_length=3)
class StopCreate(BaseModel):
    name: str = Field(min_length=2); address: str=''
    latitude: Optional[float]=None; longitude: Optional[float]=None
    radius_m: float = Field(default=50, ge=10, le=500)
class QueueJoin(BaseModel):
    stop_id: str; driver_id: str
class RideRequest(BaseModel):
    customer_name: str = Field(min_length=2); customer_phone: str = Field(min_length=6); origin: str; destination: str
    origin_lat: Optional[float]=None; origin_lng: Optional[float]=None; destination_lat: Optional[float]=None; destination_lng: Optional[float]=None
class RideAccept(BaseModel): driver_id: str
class LocationUpdate(BaseModel):
    entity_id: str = Field(min_length=3); role: str; lat: float; lng: float; ride_id: Optional[str]=None
class WhatsAppRide(BaseModel):
    customer_name: str = Field(min_length=2); customer_phone: str = Field(min_length=6); origin: str; destination: str
    confirmed: bool = False

@app.on_event('startup')
def startup(): init_db()

@app.get('/api/health')
def health(): return {'ok': True, 'service':'taxi-ya-api'}

@app.post('/api/drivers/register')
def register_driver(data: DriverRegister):
    con=db(); driver_id=str(uuid.uuid4())
    try:
        con.execute('INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?)',(driver_id,data.name,data.phone,data.license,data.plate,'pending',None,now())); con.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,'El teléfono ya está registrado')
    finally: con.close()
    return {'driver_id':driver_id,'status':'pending','message':'Registro creado. Falta activación.'}

@app.post('/api/drivers/activate')
def activate_driver(data: DriverActivate):
    if code_hash(data.code) != code_hash(ACTIVATION_CODE): raise HTTPException(403,'Código de activación incorrecto')
    con=db(); cur=con.execute('UPDATE drivers SET status="active", device_id=? WHERE id=? AND status IN ("pending","active")',(data.device_id,data.driver_id)); con.commit(); con.close()
    if cur.rowcount != 1: raise HTTPException(404,'Taxista no encontrado')
    return {'driver_id':data.driver_id,'status':'active','device_bound':True}

@app.post('/api/stops')
def create_stop(data: StopCreate):
    sid=str(uuid.uuid4()); con=db(); con.execute('INSERT INTO stops(id,name,address,latitude,longitude,radius_m,active,created_at) VALUES (?,?,?,?,?,?,?,?)',(sid,data.name,data.address,data.latitude,data.longitude,data.radius_m,1,now())); con.commit(); con.close(); return {'id':sid,**data.model_dump(),'active':True}
@app.get('/api/stops')
def list_stops():
    con=db(); rows=con.execute('SELECT * FROM stops WHERE active=1 ORDER BY name').fetchall(); con.close(); return [row_dict(r) for r in rows]
@app.put('/api/stops/{stop_id}')
def update_stop(stop_id: str, data: StopCreate):
    con=db(); cur=con.execute('UPDATE stops SET name=?,address=?,latitude=?,longitude=?,radius_m=? WHERE id=? AND active=1',(data.name,data.address,data.latitude,data.longitude,data.radius_m,stop_id)); con.commit(); row=con.execute('SELECT * FROM stops WHERE id=?',(stop_id,)).fetchone(); con.close()
    if cur.rowcount != 1: raise HTTPException(404,'Parada no encontrada')
    return row_dict(row)
@app.delete('/api/stops/{stop_id}')
def delete_stop(stop_id: str):
    con=db(); cur=con.execute('UPDATE stops SET active=0 WHERE id=? AND active=1',(stop_id,)); con.commit(); con.close()
    if cur.rowcount != 1: raise HTTPException(404,'Parada no encontrada')
    return {'ok':True}

@app.post('/api/queue/join')
def join_queue(data: QueueJoin):
    con=db(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(data.driver_id,)).fetchone(); stop=con.execute('SELECT * FROM stops WHERE id=? AND active=1',(data.stop_id,)).fetchone()
    if not driver: con.close(); raise HTTPException(403,'El taxista no está activo')
    if not stop: con.close(); raise HTTPException(404,'Parada no encontrada')
    existing=con.execute('SELECT * FROM queue_entries WHERE driver_id=? AND status="waiting"',(data.driver_id,)).fetchone()
    if existing: con.close(); raise HTTPException(409,'El taxi ya está en una cola')
    qid=str(uuid.uuid4()); con.execute('INSERT INTO queue_entries VALUES (?,?,?,?,?,?)',(qid,data.stop_id,data.driver_id,'waiting',now(),None)); con.commit()
    pos=con.execute('SELECT COUNT(*) FROM queue_entries WHERE stop_id=? AND status="waiting" AND joined_at <= (SELECT joined_at FROM queue_entries WHERE id=?)',(data.stop_id,qid)).fetchone()[0]; con.close()
    return {'queue_entry_id':qid,'position':pos,'stop_id':data.stop_id}

@app.post('/api/queue/leave')
def leave_queue(data: QueueJoin):
    con=db(); cur=con.execute('UPDATE queue_entries SET status="left", left_at=? WHERE stop_id=? AND driver_id=? AND status="waiting"',(now(),data.stop_id,data.driver_id)); con.commit(); con.close();
    if cur.rowcount != 1: raise HTTPException(404,'El taxi no está en esa cola')
    return {'ok':True}
@app.get('/api/queue/{stop_id}')
def get_queue(stop_id: str):
    con=db(); rows=con.execute('''SELECT q.id,q.stop_id,q.driver_id,q.joined_at,d.name,d.plate FROM queue_entries q JOIN drivers d ON d.id=q.driver_id WHERE q.stop_id=? AND q.status="waiting" ORDER BY q.joined_at,q.id''',(stop_id,)).fetchall(); con.close(); return [{'position':i+1,**row_dict(r)} for i,r in enumerate(rows)]

@app.post('/api/rides/request')
def request_ride(data: RideRequest):
    ride_id=str(uuid.uuid4()); con=db(); values=data.model_dump(); ride={'origin_lat':values['origin_lat'],'origin_lng':values['origin_lng'],'destination_lat':values['destination_lat'],'destination_lng':values['destination_lng']}; eta,price=estimate(ride)
    con.execute('INSERT INTO rides VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ride_id,data.customer_name,data.customer_phone,data.origin,data.destination,data.origin_lat,data.origin_lng,data.destination_lat,data.destination_lng,None,'requested',eta,price,now(),None)); con.commit(); con.close()
    return {'ride_id':ride_id,'status':'requested','eta_minutes':eta,'estimated_price':price}

@app.post('/api/rides/{ride_id}/accept')
def accept_ride(ride_id: str, data: RideAccept):
    con=db(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(data.driver_id,)).fetchone(); ride=con.execute('SELECT * FROM rides WHERE id=?',(ride_id,)).fetchone()
    if not driver: con.close(); raise HTTPException(403,'El taxista no está activo')
    if not ride: con.close(); raise HTTPException(404,'Servicio no encontrado')
    if ride['status']!='requested': con.close(); raise HTTPException(409,'El servicio ya no está disponible')
    eta,price=estimate(dict(ride)); con.execute('UPDATE rides SET driver_id=?,status="accepted",eta_minutes=?,estimated_price=?,accepted_at=? WHERE id=?',(data.driver_id,eta,price,now(),ride_id)); con.commit(); con.close()
    return {'ride_id':ride_id,'status':'accepted','driver_id':data.driver_id,'driver_name':driver['name'],'plate':driver['plate'],'eta_minutes':eta,'estimated_price':price,'whatsapp':'pending_configuration'}

@app.get('/api/rides/{ride_id}')
def get_ride(ride_id: str):
    con=db(); ride=con.execute('SELECT r.*,d.name as driver_name,d.plate FROM rides r LEFT JOIN drivers d ON d.id=r.driver_id WHERE r.id=?',(ride_id,)).fetchone(); con.close()
    if not ride: raise HTTPException(404,'Servicio no encontrado')
    return row_dict(ride)

def process_driver_presence(con, driver_id, lat, lng):
    """Actualiza la geocerca y mete/saca al taxista de la cola tras 30 segundos dentro."""
    current=now(); auto_queue=None; exited=[]
    stops=con.execute('SELECT * FROM stops WHERE active=1 AND latitude IS NOT NULL AND longitude IS NOT NULL').fetchall()
    for stop in stops:
        distance_m=(haversine(lat,lng,stop['latitude'],stop['longitude']) or 999)*1000
        inside=distance_m <= (stop['radius_m'] or 50)
        presence=con.execute('SELECT * FROM stop_presence WHERE driver_id=? AND stop_id=?',(driver_id,stop['id'])).fetchone()
        if inside:
            entered=presence['entered_at'] if presence and presence['inside'] else current
            con.execute('INSERT INTO stop_presence(driver_id,stop_id,entered_at,last_seen_at,inside) VALUES (?,?,?,?,1) ON CONFLICT(driver_id,stop_id) DO UPDATE SET entered_at=excluded.entered_at,last_seen_at=excluded.last_seen_at,inside=1',(driver_id,stop['id'],entered,current))
            elapsed=(datetime.fromisoformat(current)-datetime.fromisoformat(entered)).total_seconds()
            if elapsed >= 30:
                existing=con.execute('SELECT * FROM queue_entries WHERE driver_id=? AND status="waiting"',(driver_id,)).fetchone()
                same=con.execute('SELECT * FROM queue_entries WHERE driver_id=? AND stop_id=? AND status="waiting"',(driver_id,stop['id'])).fetchone()
                if not existing and not same:
                    qid=str(uuid.uuid4()); con.execute('INSERT INTO queue_entries VALUES (?,?,?,?,?,?)',(qid,stop['id'],driver_id,'waiting',current,None)); same=con.execute('SELECT * FROM queue_entries WHERE id=?',(qid,)).fetchone()
                if same:
                    position=con.execute('SELECT COUNT(*) FROM queue_entries WHERE stop_id=? AND status="waiting" AND joined_at <= ?',(stop['id'],same['joined_at'])).fetchone()[0]
                    auto_queue={'queue_entry_id':same['id'],'stop_id':stop['id'],'stop_name':stop['name'],'position':position,'distance_m':round(distance_m)}
        elif presence and presence['inside']:
            con.execute('UPDATE stop_presence SET inside=0,last_seen_at=? WHERE driver_id=? AND stop_id=?',(current,driver_id,stop['id']))
            cur=con.execute('UPDATE queue_entries SET status="left",left_at=? WHERE driver_id=? AND stop_id=? AND status="waiting" AND joined_at>=?', (current,driver_id,stop['id'],presence['entered_at']))
            if cur.rowcount: exited.append(stop['id'])
    return auto_queue,exited

@app.post('/api/locations')
def update_location(data: LocationUpdate):
    if data.role not in ('customer', 'driver', 'admin'):
        raise HTTPException(400, 'Perfil de ubicación no válido')
    con=db(); con.execute('''INSERT INTO locations(entity_id,role,lat,lng,ride_id,updated_at) VALUES (?,?,?,?,?,?)
        ON CONFLICT(entity_id,role) DO UPDATE SET lat=excluded.lat,lng=excluded.lng,ride_id=excluded.ride_id,updated_at=excluded.updated_at''',
        (data.entity_id, data.role, data.lat, data.lng, data.ride_id, now()))
    auto_queue,exited=process_driver_presence(con,data.entity_id,data.lat,data.lng) if data.role=='driver' else (None,[])
    con.commit(); con.close()
    return {'ok': True, 'sharing': True, 'updated_at': now(), 'auto_queue':auto_queue, 'exited_stop_ids':exited}

@app.get('/api/locations')
def list_locations(ride_id: Optional[str]=None):
    con=db();
    if ride_id:
        rows=con.execute('SELECT * FROM locations WHERE ride_id=? ORDER BY updated_at DESC',(ride_id,)).fetchall()
    else:
        rows=con.execute('SELECT * FROM locations ORDER BY updated_at DESC').fetchall()
    con.close(); return [row_dict(r) for r in rows]

@app.post('/api/whatsapp/request')
def whatsapp_request(data: WhatsAppRide):
    """Puente preparado para WhatsApp Business: la IA confirma y luego crea el mismo servicio que la app."""
    eta, price = estimate({'origin_lat': None, 'origin_lng': None, 'destination_lat': None, 'destination_lng': None})
    if not data.confirmed:
        return {'status':'awaiting_confirmation','origin':data.origin,'destination':data.destination,
                'eta_minutes':eta,'estimated_price':price,
                'message':f'Confirma Taxi Ya: recogida en {data.origin}, destino {data.destination}. ¿Confirmas?'}
    ride=request_ride(RideRequest(customer_name=data.customer_name, customer_phone=data.customer_phone,
        origin=data.origin, destination=data.destination))
    return {'status':'created','channel':'whatsapp','ride':ride}

@app.get('/api/admin/summary')
def admin_summary():
    con=db(); result={'stops':con.execute('SELECT COUNT(*) FROM stops WHERE active=1').fetchone()[0],'active_drivers':con.execute('SELECT COUNT(*) FROM drivers WHERE status="active"').fetchone()[0],'today_rides':con.execute('SELECT COUNT(*) FROM rides WHERE date(created_at)=date("now")').fetchone()[0]}; con.close(); return result

# The same service serves the mobile-friendly web app when deployed.
app.mount('/', StaticFiles(directory=Path(__file__).parent, html=True), name='static')
