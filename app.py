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
CREATE TABLE IF NOT EXISTS drivers (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, license TEXT, plate TEXT, status TEXT NOT NULL DEFAULT 'pending', device_id TEXT, created_at TEXT NOT NULL, available INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stops (id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT, latitude REAL, longitude REAL, radius_m REAL NOT NULL DEFAULT 50, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue_entries (id TEXT PRIMARY KEY, stop_id TEXT NOT NULL, driver_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', joined_at TEXT NOT NULL, left_at TEXT, UNIQUE(stop_id, driver_id), FOREIGN KEY(stop_id) REFERENCES stops(id), FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS stop_presence (driver_id TEXT NOT NULL, stop_id TEXT NOT NULL, entered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, inside INTEGER NOT NULL DEFAULT 1, outside_since TEXT, PRIMARY KEY(driver_id, stop_id), FOREIGN KEY(stop_id) REFERENCES stops(id), FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS rides (id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, customer_phone TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, origin_lat REAL, origin_lng REAL, destination_lat REAL, destination_lng REAL, driver_id TEXT, status TEXT NOT NULL DEFAULT 'requested', eta_minutes INTEGER, estimated_price REAL, created_at TEXT NOT NULL, accepted_at TEXT, FOREIGN KEY(driver_id) REFERENCES drivers(id));
CREATE TABLE IF NOT EXISTS locations (entity_id TEXT NOT NULL, role TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, ride_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(entity_id, role));
'''

def now(): return datetime.now(timezone.utc).isoformat()
def db():
    # Render Free can receive GPS and registration requests at the same time.
    # A busy timeout lets short concurrent writes finish instead of failing.
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=60000')
    con.execute('PRAGMA foreign_keys=ON')
    return con
def init_db():
    con=db()
    # Configure WAL once at startup, not on every GPS request. Repeating this
    # write-pragmas on each connection can itself cause SQLITE_BUSY.
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    stop_cols={r['name'] for r in con.execute('PRAGMA table_info(stops)').fetchall()}
    for name,definition in [('latitude','REAL'),('longitude','REAL'),('radius_m','REAL NOT NULL DEFAULT 50')]:
        if name not in stop_cols: con.execute(f'ALTER TABLE stops ADD COLUMN {name} {definition}')
    driver_cols={r['name'] for r in con.execute('PRAGMA table_info(drivers)').fetchall()}
    for name,definition in [('available','INTEGER NOT NULL DEFAULT 0'),('busy','INTEGER NOT NULL DEFAULT 0')]:
        if name not in driver_cols: con.execute(f'ALTER TABLE drivers ADD COLUMN {name} {definition}')
    presence_cols={r['name'] for r in con.execute('PRAGMA table_info(stop_presence)').fetchall()}
    if 'outside_since' not in presence_cols: con.execute('ALTER TABLE stop_presence ADD COLUMN outside_since TEXT')
    con.execute('UPDATE stops SET radius_m=50 WHERE radius_m IS NULL')
    con.commit(); con.close()

def row_dict(row): return dict(row) if row else None
def code_hash(code): return hashlib.sha256(code.encode()).hexdigest()

def haversine(a_lat,a_lng,b_lat,b_lng):
    if None in (a_lat,a_lng,b_lat,b_lng): return None
    r=6371; p=math.pi/180; dlat=(b_lat-a_lat)*p; dlng=(b_lng-a_lng)*p
    x=math.sin(dlat/2)**2+math.cos(a_lat*p)*math.cos(b_lat*p)*math.sin(dlng/2)**2
    return r*2*math.asin(math.sqrt(x))

CENTRAL_FARE_NAME='Entrevías'; CENTRAL_FARE_LAT=37.969; CENTRAL_FARE_LNG=-1.217; SERVICE_ZONE_KM=5.0
ROUTE_CACHE={}
def road_distance_km(points):
    clean=[(round(float(lat),6),round(float(lng),6)) for lat,lng in points if lat is not None and lng is not None]
    if len(clean)<2: return None
    key=';'.join(f'{lng},{lat}' for lat,lng in clean)
    if key in ROUTE_CACHE: return ROUTE_CACHE[key]
    try:
        async_url='https://router.project-osrm.org/route/v1/driving/'+key
        with httpx.Client(timeout=6,headers={'User-Agent':'TaxiYa-MVP/1.0'}) as client:
            response=client.get(async_url,params={'overview':'false','alternatives':'false'})
            response.raise_for_status(); km=response.json()['routes'][0]['distance']/1000
        ROUTE_CACHE[key]=km; return km
    except Exception:
        return None

def estimate_km(km):
    km=km if km is not None else 4.0
    eta=max(3, math.ceil(km/0.45))
    price=4.20 + km*1.05 + eta*0.18
    return eta, round(price,2)

def estimate(ride):
    km=road_distance_km([(ride.get('origin_lat'),ride.get('origin_lng')),(ride.get('destination_lat'),ride.get('destination_lng'))])
    if km is None: km=haversine(ride.get('origin_lat'),ride.get('origin_lng'),ride.get('destination_lat'),ride.get('destination_lng'))
    return estimate_km(km)

def fare_reference(con):
    row=con.execute('''SELECT latitude,longitude FROM stops WHERE active=1 AND lower(replace(name,'í','i'))=lower('entrevias') LIMIT 1''').fetchone()
    if row and row['latitude'] is not None and row['longitude'] is not None: return row['latitude'],row['longitude'],CENTRAL_FARE_NAME
    return CENTRAL_FARE_LAT,CENTRAL_FARE_LNG,CENTRAL_FARE_NAME

def estimate_from_central(con, ride):
    central_lat,central_lng,name=fare_reference(con)
    origin=(ride.get('origin_lat'),ride.get('origin_lng')); destination=(ride.get('destination_lat'),ride.get('destination_lng'))
    if origin[0] is not None and origin[1] is not None and destination[0] is not None and destination[1] is not None:
        total=road_distance_km([(central_lat,central_lng),origin,destination])
        if total is None:
            total=(haversine(central_lat,central_lng,origin[0],origin[1]) or 0)+(haversine(origin[0],origin[1],destination[0],destination[1]) or 0)
    else:
        target=destination if destination[0] is not None and destination[1] is not None else origin
        total=road_distance_km([(central_lat,central_lng),target]) if target[0] is not None and target[1] is not None else None
        if total is None and target[0] is not None and target[1] is not None: total=haversine(central_lat,central_lng,target[0],target[1])
    return estimate_km(total),name

def driver_dispatch(con, origin_lat, origin_lng, exclude_driver_id=None):
    if origin_lat is None or origin_lng is None: return None
    for q in queue_rows(con):
        if exclude_driver_id and q['driver_id']==exclude_driver_id: continue
        loc=con.execute('SELECT lat,lng FROM locations WHERE entity_id=? AND role="driver"',(q['driver_id'],)).fetchone()
        if loc and (haversine(origin_lat,origin_lng,loc['lat'],loc['lng']) or 999) <= SERVICE_ZONE_KM:
            return {'driver_id':q['driver_id'],'mode':'queue','distance_km':round(haversine(origin_lat,origin_lng,loc['lat'],loc['lng']),2)}
    rows=con.execute('''SELECT l.entity_id,l.lat,l.lng FROM locations l JOIN drivers d ON d.id=l.entity_id
        WHERE l.role="driver" AND d.status="active" AND d.available=1 AND d.busy=0''').fetchall()
    candidates=[]
    for row in rows:
        if exclude_driver_id and row['entity_id']==exclude_driver_id: continue
        distance=haversine(origin_lat,origin_lng,row['lat'],row['lng'])
        if distance is not None and distance<=SERVICE_ZONE_KM: candidates.append((distance,row['entity_id']))
    if not candidates: return None
    distance,driver_id=min(candidates); return {'driver_id':driver_id,'mode':'nearest','distance_km':round(distance,2)}

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
class RideTimeout(BaseModel): driver_id: str
class RideStatus(BaseModel): status: str
class LocationUpdate(BaseModel):
    entity_id: str = Field(min_length=3); role: str; lat: float; lng: float; ride_id: Optional[str]=None; available: Optional[bool]=None
class DriverAvailability(BaseModel):
    available: bool
class WhatsAppRide(BaseModel):
    customer_name: str = Field(min_length=2); customer_phone: str = Field(min_length=6); origin: str; destination: str
    confirmed: bool = False

@app.on_event('startup')
def startup(): init_db()

@app.get('/api/health')
def health(): return {'ok': True, 'service':'taxi-ya-api'}

GEOCODE_CACHE={}
@app.get('/api/geocode')
async def geocode(q: str):
    query=' '.join((q or '').split())
    if len(query)<3: return []
    key=query.lower()
    if key in GEOCODE_CACHE: return GEOCODE_CACHE[key]
    search=query if any(x in key for x in ('murcia','alcantarilla','españa','spain')) else f'{query}, Alcantarilla, Murcia, España'
    try:
        async with httpx.AsyncClient(timeout=8,headers={'User-Agent':'TaxiYa-MVP/1.0'}) as client:
            response=await client.get('https://photon.komoot.io/api/',params={'q':search,'limit':5,'lat':CENTRAL_FARE_LAT,'lon':CENTRAL_FARE_LNG})
            response.raise_for_status(); features=response.json().get('features',[])
        results=[]
        for feature in features:
            props=feature.get('properties',{}); coords=feature.get('geometry',{}).get('coordinates',[])
            if len(coords)<2: continue
            parts=[]
            if props.get('name'): parts.append(props['name'] + (f" {props['housenumber']}" if props.get('housenumber') else ''))
            parts += [props.get(k) for k in ('locality','city','state','postcode','country') if props.get(k)]
            results.append({'display_name':', '.join(dict.fromkeys(parts)),'lat':float(coords[1]),'lng':float(coords[0]),'type':props.get('type','')})
        if not results: raise RuntimeError('sin resultados en Photon')
    except Exception:
        try:
            async with httpx.AsyncClient(timeout=8,headers={'User-Agent':'TaxiYa-MVP/1.0'}) as client:
                response=await client.get('https://nominatim.openstreetmap.org/search',params={'q':search,'format':'jsonv2','addressdetails':1,'limit':5,'countrycodes':'es','accept-language':'es'})
                response.raise_for_status(); raw=response.json()
            results=[{'display_name':x.get('display_name',''),'lat':float(x['lat']),'lng':float(x['lon']),'type':x.get('type','')} for x in raw if x.get('lat') and x.get('lon')]
        except Exception:
            results=[]
    GEOCODE_CACHE[key]=results
    return results

@app.post('/api/drivers/register')
def register_driver(data: DriverRegister):
    con=db(); driver_id=str(uuid.uuid4())
    try:
        con.execute('INSERT INTO drivers VALUES (?,?,?,?,?,?,?,?,?,?)',(driver_id,data.name,data.phone,data.license,data.plate,'pending',None,now(),0,0)); con.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,'El teléfono ya está registrado')
    finally: con.close()
    return {'driver_id':driver_id,'status':'pending','message':'Registro creado. Falta activación.'}

@app.post('/api/drivers/activate')
def activate_driver(data: DriverActivate):
    if code_hash(data.code) != code_hash(ACTIVATION_CODE): raise HTTPException(403,'Código de activación incorrecto')
    con=db(); cur=con.execute('UPDATE drivers SET status="active", device_id=?, available=0, busy=0 WHERE id=? AND status IN ("pending","active")',(data.device_id,data.driver_id)); con.commit(); con.close()
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

def queue_rows(con, stop_id=None):
    sql='''SELECT q.id,q.stop_id,q.driver_id,q.joined_at,d.name,d.plate FROM queue_entries q JOIN drivers d ON d.id=q.driver_id WHERE q.status="waiting"'''
    params=[]
    if stop_id: sql+=' AND q.stop_id=?'; params.append(stop_id)
    return con.execute(sql+' ORDER BY q.joined_at,q.id',params).fetchall()

def queue_position(con, entry_id):
    row=con.execute('SELECT joined_at,id FROM queue_entries WHERE id=?',(entry_id,)).fetchone()
    if not row: return None
    return con.execute('SELECT COUNT(*) FROM queue_entries WHERE status="waiting" AND (joined_at<? OR (joined_at=? AND id<=?))',(row['joined_at'],row['joined_at'],row['id'])).fetchone()[0]

def queue_entry_for_driver(con, driver_id):
    return con.execute('SELECT * FROM queue_entries WHERE driver_id=? AND status="waiting" ORDER BY joined_at,id LIMIT 1',(driver_id,)).fetchone()

def activate_queue_entry(con, stop_id, driver_id, joined_at):
    old=con.execute('SELECT * FROM queue_entries WHERE stop_id=? AND driver_id=? ORDER BY joined_at DESC LIMIT 1',(stop_id,driver_id)).fetchone()
    if old:
        con.execute('UPDATE queue_entries SET status="waiting",joined_at=?,left_at=NULL WHERE id=?',(joined_at,old['id']))
        return con.execute('SELECT * FROM queue_entries WHERE id=?',(old['id'],)).fetchone()
    qid=str(uuid.uuid4()); con.execute('INSERT INTO queue_entries VALUES (?,?,?,?,?,?)',(qid,stop_id,driver_id,'waiting',joined_at,None))
    return con.execute('SELECT * FROM queue_entries WHERE id=?',(qid,)).fetchone()

@app.post('/api/drivers/{driver_id}/availability')
def set_driver_availability(driver_id: str, data: DriverAvailability):
    con=db(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(driver_id,)).fetchone()
    if not driver: con.close(); raise HTTPException(404,'Taxista no encontrado')
    busy=0 if data.available else 1
    con.execute('UPDATE drivers SET available=?,busy=? WHERE id=?',(1 if data.available else 0,busy,driver_id))
    removed=[]
    if not data.available:
        rows=con.execute('SELECT stop_id FROM queue_entries WHERE driver_id=? AND status="waiting"',(driver_id,)).fetchall()
        removed=[r['stop_id'] for r in rows]
        con.execute('UPDATE queue_entries SET status="left",left_at=? WHERE driver_id=? AND status="waiting"',(now(),driver_id))
    con.commit(); con.close()
    return {'ok':True,'driver_id':driver_id,'available':data.available,'busy':not data.available,'removed_stop_ids':removed}

@app.post('/api/queue/join')
def join_queue(data: QueueJoin):
    con=db(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(data.driver_id,)).fetchone(); stop=con.execute('SELECT * FROM stops WHERE id=? AND active=1',(data.stop_id,)).fetchone()
    if not driver: con.close(); raise HTTPException(403,'El taxista no está activo')
    if not stop: con.close(); raise HTTPException(404,'Parada no encontrada')
    if not driver['available'] or driver['busy']: con.close(); raise HTTPException(409,'El taxi debe estar libre para entrar en la cola')
    existing=queue_entry_for_driver(con,data.driver_id)
    if existing: con.close(); raise HTTPException(409,'El taxi ya está en la cola unificada')
    entry=activate_queue_entry(con,data.stop_id,data.driver_id,now()); con.commit(); pos=queue_position(con,entry['id']); con.close()
    return {'queue_entry_id':entry['id'],'position':pos,'stop_id':data.stop_id,'unified':True}

@app.post('/api/queue/leave')
def leave_queue(data: QueueJoin):
    con=db(); cur=con.execute('UPDATE queue_entries SET status="left", left_at=? WHERE driver_id=? AND status="waiting"',(now(),data.driver_id)); con.commit(); con.close();
    if cur.rowcount != 1: raise HTTPException(404,'El taxi no está en la cola')
    return {'ok':True}
@app.get('/api/queue')
def get_unified_queue():
    con=db(); rows=queue_rows(con); result=[{'position':i+1,**row_dict(r)} for i,r in enumerate(rows)]; con.close(); return result
@app.get('/api/queue/{stop_id}')
def get_queue(stop_id: str):
    con=db(); rows=queue_rows(con,stop_id); result=[]
    for r in rows: result.append({'position':queue_position(con,r['id']),**row_dict(r)})
    con.close(); return result

@app.post('/api/rides/request')
def request_ride(data: RideRequest):
    ride_id=str(uuid.uuid4()); con=db(); values=data.model_dump(); ride={'origin_lat':values['origin_lat'],'origin_lng':values['origin_lng'],'destination_lat':values['destination_lat'],'destination_lng':values['destination_lng']}; (eta,price),fare_name=estimate_from_central(con,ride); dispatch=driver_dispatch(con,data.origin_lat,data.origin_lng)
    assigned_driver=dispatch['driver_id'] if dispatch else None
    con.execute('INSERT INTO rides VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ride_id,data.customer_name,data.customer_phone,data.origin,data.destination,data.origin_lat,data.origin_lng,data.destination_lat,data.destination_lng,assigned_driver,'requested',eta,price,now(),None)); con.commit(); con.close()
    return {'ride_id':ride_id,'status':'requested','eta_minutes':eta,'estimated_price':price,'fare_reference':fare_name,'dispatch':dispatch,'zone_km':SERVICE_ZONE_KM}

@app.post('/api/rides/{ride_id}/accept')
def accept_ride(ride_id: str, data: RideAccept):
    con=db(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(data.driver_id,)).fetchone(); ride=con.execute('SELECT * FROM rides WHERE id=?',(ride_id,)).fetchone()
    if not driver: con.close(); raise HTTPException(403,'El taxista no está activo')
    if not ride: con.close(); raise HTTPException(404,'Servicio no encontrado')
    if ride['status']!='requested': con.close(); raise HTTPException(409,'El servicio ya no está disponible')
    if ride['driver_id'] and ride['driver_id']!=data.driver_id: con.close(); raise HTTPException(409,'El servicio está asignado a otro taxi')
    if not driver['available'] or driver['busy']: con.close(); raise HTTPException(409,'El taxi no está libre')
    (eta,price),_=estimate_from_central(con,dict(ride)); con.execute('UPDATE rides SET driver_id=?,status="accepted",eta_minutes=?,estimated_price=?,accepted_at=? WHERE id=?',(data.driver_id,eta,price,now(),ride_id)); con.execute('UPDATE drivers SET available=0,busy=1 WHERE id=?',(data.driver_id,)); con.execute('UPDATE queue_entries SET status="left",left_at=? WHERE driver_id=? AND status="waiting"',(now(),data.driver_id)); con.commit(); con.close()
    return {'ride_id':ride_id,'status':'accepted','driver_id':data.driver_id,'driver_name':driver['name'],'plate':driver['plate'],'eta_minutes':eta,'estimated_price':price,'availability':'busy','whatsapp':'pending_configuration'}

@app.post('/api/rides/{ride_id}/timeout')
def timeout_ride(ride_id: str, data: RideTimeout):
    con=db(); ride=con.execute('SELECT * FROM rides WHERE id=?',(ride_id,)).fetchone(); driver=con.execute('SELECT * FROM drivers WHERE id=? AND status="active"',(data.driver_id,)).fetchone()
    if not ride: con.close(); raise HTTPException(404,'Servicio no encontrado')
    if not driver: con.close(); raise HTTPException(403,'El taxista no está activo')
    if ride['status']!='requested' or ride['driver_id']!=data.driver_id:
        con.close(); raise HTTPException(409,'La solicitud ya cambió de taxista o fue aceptada')
    current=now()
    # El taxista que no responde pasa al final de la cola unificada.
    con.execute('UPDATE queue_entries SET joined_at=?,left_at=NULL WHERE driver_id=? AND status="waiting"',(current,data.driver_id))
    dispatch=driver_dispatch(con,ride['origin_lat'],ride['origin_lng'],exclude_driver_id=data.driver_id)
    next_driver=dispatch['driver_id'] if dispatch else None
    con.execute('UPDATE rides SET driver_id=? WHERE id=? AND status="requested"',(next_driver,ride_id)); con.commit(); con.close()
    return {'ride_id':ride_id,'status':'requested','previous_driver_id':data.driver_id,'next_driver_id':next_driver,'dispatch':dispatch,'zone_km':SERVICE_ZONE_KM}

@app.get('/api/rides/open')
def open_rides(driver_id: Optional[str]=None):
    con=db()
    if driver_id: rows=con.execute('SELECT * FROM rides WHERE status="requested" AND (driver_id=? OR driver_id IS NULL) ORDER BY created_at,id',(driver_id,)).fetchall()
    else: rows=con.execute('SELECT * FROM rides WHERE status="requested" ORDER BY created_at,id').fetchall()
    con.close(); return [row_dict(r) for r in rows]

@app.post('/api/rides/{ride_id}/status')
def update_ride_status(ride_id: str, data: RideStatus):
    transitions={'accepted':'to_pickup','to_pickup':'picked_up','picked_up':'completed'}
    con=db(); ride=con.execute('SELECT * FROM rides WHERE id=?',(ride_id,)).fetchone()
    if not ride: con.close(); raise HTTPException(404,'Servicio no encontrado')
    if data.status not in ('to_pickup','picked_up','completed') or transitions.get(ride['status'])!=data.status:
        con.close(); raise HTTPException(409,'Estado de servicio no válido')
    con.execute('UPDATE rides SET status=? WHERE id=?',(data.status,ride_id))
    if data.status=='completed' and ride['driver_id']:
        # Al terminar, el taxista queda libre inmediatamente y podrá volver a la cola
        # en cuanto su siguiente actualización GPS confirme que está en una parada.
        con.execute('UPDATE drivers SET available=1,busy=0 WHERE id=?',(ride['driver_id'],))
    con.commit(); updated=con.execute('SELECT r.*,d.name as driver_name,d.plate,d.available as driver_available,d.busy as driver_busy FROM rides r LEFT JOIN drivers d ON d.id=r.driver_id WHERE r.id=?',(ride_id,)).fetchone(); con.close()
    return row_dict(updated)

@app.get('/api/rides/driver/{driver_id}')
def driver_rides(driver_id: str):
    con=db(); rows=con.execute('SELECT * FROM rides WHERE driver_id=? AND status!="completed" ORDER BY created_at DESC',(driver_id,)).fetchall(); con.close(); return [row_dict(r) for r in rows]

@app.get('/api/rides/{ride_id}')
def get_ride(ride_id: str):
    con=db(); ride=con.execute('SELECT r.*,d.name as driver_name,d.plate FROM rides r LEFT JOIN drivers d ON d.id=r.driver_id WHERE r.id=?',(ride_id,)).fetchone(); con.close()
    if not ride: raise HTTPException(404,'Servicio no encontrado')
    return row_dict(ride)

def process_driver_presence(con, driver_id, lat, lng):
    """Geocerca automática: 30 s dentro añade a la cola unificada y 30 s fuera lo elimina."""
    current=now(); auto_queue=None; exited=[]
    driver=con.execute('SELECT available,busy FROM drivers WHERE id=?',(driver_id,)).fetchone()
    can_queue=bool(driver and driver['available'] and not driver['busy'])
    stops=con.execute('SELECT * FROM stops WHERE active=1 AND latitude IS NOT NULL AND longitude IS NOT NULL').fetchall()
    for stop in stops:
        distance_km=haversine(lat,lng,stop['latitude'],stop['longitude']); distance_m=(999 if distance_km is None else distance_km*1000)
        inside=distance_m <= (stop['radius_m'] or 50)
        presence=con.execute('SELECT * FROM stop_presence WHERE driver_id=? AND stop_id=?',(driver_id,stop['id'])).fetchone()
        if inside:
            entered=presence['entered_at'] if presence and presence['inside'] else current
            con.execute('''INSERT INTO stop_presence(driver_id,stop_id,entered_at,last_seen_at,inside,outside_since) VALUES (?,?,?,?,1,NULL)
                ON CONFLICT(driver_id,stop_id) DO UPDATE SET entered_at=excluded.entered_at,last_seen_at=excluded.last_seen_at,inside=1,outside_since=NULL''',(driver_id,stop['id'],entered,current))
            elapsed=(datetime.fromisoformat(current)-datetime.fromisoformat(entered)).total_seconds()
            if can_queue and elapsed >= 30 and not queue_entry_for_driver(con,driver_id):
                entry=activate_queue_entry(con,stop['id'],driver_id,entered)
                auto_queue={'queue_entry_id':entry['id'],'stop_id':stop['id'],'stop_name':stop['name'],'position':queue_position(con,entry['id']),'distance_m':round(distance_m)}
            elif can_queue and elapsed >= 30:
                existing=queue_entry_for_driver(con,driver_id)
                if existing:
                    auto_queue={'queue_entry_id':existing['id'],'stop_id':existing['stop_id'],'stop_name':stop['name'] if existing['stop_id']==stop['id'] else 'Cola unificada','position':queue_position(con,existing['id']),'distance_m':round(distance_m)}
        elif presence and presence['inside']:
            if not presence['outside_since']:
                con.execute('UPDATE stop_presence SET outside_since=?,last_seen_at=? WHERE driver_id=? AND stop_id=?',(current,current,driver_id,stop['id']))
            else:
                outside_elapsed=(datetime.fromisoformat(current)-datetime.fromisoformat(presence['outside_since'])).total_seconds()
                if outside_elapsed >= 30:
                    con.execute('UPDATE stop_presence SET inside=0,last_seen_at=?,outside_since=NULL WHERE driver_id=? AND stop_id=?',(current,driver_id,stop['id']))
                    cur=con.execute('UPDATE queue_entries SET status="left",left_at=? WHERE driver_id=? AND stop_id=? AND status="waiting"',(current,driver_id,stop['id']))
                    if cur.rowcount: exited.append(stop['id'])
    return auto_queue,exited

@app.post('/api/locations')
def update_location(data: LocationUpdate):
    if data.role not in ('customer', 'driver', 'admin'):
        raise HTTPException(400, 'Perfil de ubicación no válido')
    con=db(); current=now()
    if data.role=='driver' and data.available is not None:
        con.execute('UPDATE drivers SET available=?,busy=? WHERE id=? AND status="active"',(1 if data.available else 0,0 if data.available else 1,data.entity_id))
        if not data.available: con.execute('UPDATE queue_entries SET status="left",left_at=? WHERE driver_id=? AND status="waiting"',(current,data.entity_id))
    con.execute('''INSERT INTO locations(entity_id,role,lat,lng,ride_id,updated_at) VALUES (?,?,?,?,?,?)
        ON CONFLICT(entity_id,role) DO UPDATE SET lat=excluded.lat,lng=excluded.lng,ride_id=excluded.ride_id,updated_at=excluded.updated_at''',
        (data.entity_id, data.role, data.lat, data.lng, data.ride_id, current))
    auto_queue,exited=process_driver_presence(con,data.entity_id,data.lat,data.lng) if data.role=='driver' else (None,[])
    current_entry=queue_entry_for_driver(con,data.entity_id) if data.role=='driver' else None
    queue_state={'queue_entry_id':current_entry['id'],'stop_id':current_entry['stop_id'],'position':queue_position(con,current_entry['id'])} if current_entry else None
    con.commit(); con.close()
    return {'ok': True, 'sharing': True, 'updated_at': current, 'auto_queue':auto_queue, 'queue':queue_state, 'exited_stop_ids':exited}

@app.get('/api/locations')
def list_locations(ride_id: Optional[str]=None):
    con=db(); base='''SELECT l.*,d.available,d.busy FROM locations l LEFT JOIN drivers d ON d.id=l.entity_id AND l.role="driver"'''
    if ride_id: rows=con.execute(base+' WHERE l.ride_id=? ORDER BY l.updated_at DESC',(ride_id,)).fetchall()
    else: rows=con.execute(base+' ORDER BY l.updated_at DESC').fetchall()
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
