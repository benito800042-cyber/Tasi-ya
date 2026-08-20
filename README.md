# Taxi Ya — backend MVP

Base real del siguiente paso: API FastAPI + SQLite para Alcantarilla (Murcia).

Incluye:
- Registro y activación de taxistas con código configurable (por defecto 123456).
- Vinculación del taxista a un dispositivo.
- Paradas y cola unificada ordenada por hora del servidor.
- Solicitud y aceptación de servicios.
- Estimación de distancia, tiempo y precio cuando hay coordenadas GPS.
- Resumen para administración.

## Arranque local

```bash
uv venv .venv
uv pip sync requirements.txt
uv run uvicorn app:app --reload
```

Documentación interactiva: `http://127.0.0.1:8000/docs`

## Para producción

Faltan conectar un PostgreSQL/hosting, autenticación con sesiones o JWT, proveedor de mapas/rutas, geolocalización del móvil y WhatsApp Business Cloud API. Las credenciales van en variables de entorno; nunca se deben guardar en el código.
