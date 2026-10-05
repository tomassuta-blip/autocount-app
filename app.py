import io
import os
import re
import zipfile
import base64
import difflib
import json
import sqlite3
import xml.etree.ElementTree as ET
import pandas as pd
import requests
import streamlit as st
from datetime import datetime
from typing import Dict, List, Any
from pypdf import PdfReader
from fpdf import FPDF
from openai import OpenAI
import math
import time
import asyncio
import threading
import hashlib
import hmac
import html

# ---------- Zona horaria Colombia (en la nube el servidor trabaja en UTC) ----------
os.environ["TZ"] = "America/Bogota"
if hasattr(time, "tzset"):
    try: time.tzset()
    except Exception: pass

def _secret(nombre, defecto=""):
    """Lee un secreto de st.secrets (Streamlit Cloud) o de una variable de entorno. Si no existe, devuelve el valor por defecto."""
    try:
        v = st.secrets.get(nombre)
        if v: return str(v)
    except Exception:
        pass
    return os.environ.get(nombre, defecto)

# ==========================================
# WEBHOOK GOOGLE SHEETS + FILTRO DE FECHA
# ==========================================
WEBHOOK_SHEETS_URL = _secret("WEBHOOK_SHEETS_URL", "https://script.google.com/macros/s/AKfycbx0Qo-7qO-0RdJfFE4amZfcJQeIE82BJRt3SPX6kLYvp2MJ-UOtCLM1-R_XAwIcD1Tg/exec")
FECHA_MINIMA_RECEPCION = "2026-09-01"
WEBHOOK_VERSION_ESPERADA = "v5-link-nativo"   # versión del Apps Script que debe estar publicada

def _limpiar_valor_webhook(v):
    """Evita NaN / tipos numpy que rompen el JSON."""
    try:
        if v is None: return ""
        if isinstance(v, float) and math.isnan(v): return ""
        if hasattr(v, "item"): v = v.item()  # numpy -> python
        return v
    except Exception:
        return str(v)

def _post_webhook_sync(fila: list, etiqueta: str = "", adjuntos=None):
    """POST real al Apps Script. Devuelve {ok, msg, aviso, etiqueta}. 'aviso' = la fila llegó pero algo secundario (carpeta/archivos) falló."""
    fila_ok = [_limpiar_valor_webhook(v) for v in fila]
    payload = {"fila": fila_ok}
    if adjuntos: payload["archivos"] = adjuntos
    resultado = {"ok": False, "msg": "", "aviso": "", "etiqueta": etiqueta}
    try:
        res = requests.post(WEBHOOK_SHEETS_URL, json=payload, timeout=120, allow_redirects=True)
        try: j = res.json()
        except Exception:
            resultado["msg"] = (f"HTTP {res.status_code} y la respuesta NO es JSON. Casi seguro el despliegue no es "
                                f"'Cualquier persona' o no se publicó la versión nueva. Inicio de respuesta: {res.text[:120]!r}")
            j = None
        if j is not None:
            estado = str(j.get("status", ""))
            if estado.startswith("Creado") or estado.startswith("Actualizado"):
                resultado["ok"] = True
                partes = [f"{estado} (fila {j.get('fila', '?')})"]
                if j.get("carpeta_estado"): partes.append(f"📁 carpeta {j['carpeta_estado']}")
                if j.get("archivos_subidos"): partes.append(f"📎 {j['archivos_subidos']} archivo(s) guardados")
                resultado["msg"] = " · ".join(partes)
                avisos = []
                if j.get("carpeta_error"): avisos.append(f"No se pudo crear/usar la carpeta de Drive: {j['carpeta_error']}")
                if j.get("archivos_error"): avisos.append("Archivos que no se guardaron: " + "; ".join(map(str, j["archivos_error"])))
                if j.get("version") != WEBHOOK_VERSION_ESPERADA:
                    avisos.append(f"El Apps Script publicado es la versión '{j.get('version', 'antigua (sin número)')}' y se esperaba '{WEBHOOK_VERSION_ESPERADA}'. Pega el código nuevo y publica una NUEVA VERSIÓN (Implementar > Administrar implementaciones > lápiz > Nueva versión).")
                resultado["aviso"] = "  |  ".join(avisos)
            else: resultado["msg"] = f"Respuesta del script: {j}"
    except Exception as e:
        resultado["msg"] = f"Error de red: {e}"
    print(f"[Webhook Sheets] {etiqueta} -> ok={resultado['ok']} | {resultado['msg']} | aviso={resultado['aviso']}")
    return resultado

async def enviar_fila_webhook_async(fila: list, etiqueta: str = "", adjuntos=None):
    """Envía una fila de 26 datos (A-Z) al webhook de Google Sheets."""
    return await asyncio.to_thread(_post_webhook_sync, fila, etiqueta, adjuntos)

def destino_es_cxp(clasificacion):
    """Solo lo que se causa como CXP viaja a Google Sheets. Tarjeta de crédito y caja menor se quedan únicamente en la app.
    Sin dato (documentos antiguos) se trata como CXP, que es el comportamiento de siempre."""
    t = str(clasificacion or "").strip().lower()
    return not (t.startswith("tarjeta") or t.startswith("caja"))

def fila_tesoreria_va_a_sheets(raw_data):
    """¿Esta fila de Tesorería nació como CXP? Las filas nuevas traen la marca '_destino'; las anteriores de tarjeta se reconocen por su Banco Girador."""
    try: raw = json.loads(raw_data) if isinstance(raw_data, str) else dict(raw_data or {})
    except Exception: raw = {}
    if raw.get("_destino"): return destino_es_cxp(raw["_destino"])
    return str(raw.get("Banco Girador", "")).strip().lower() != "tarjeta de crédito"

def enviar_fila_webhook(fila: list, etiqueta: str = "", adjuntos=None, destino="CXP"):
    """Puente síncrono para Streamlit. Guarda el resultado para mostrarlo en pantalla tras el rerun.
    Si el destino del documento no es CXP (tarjeta, caja menor) NO se envía nada a la hoja."""
    if not destino_es_cxp(destino):
        print(f"[Webhook Sheets] {etiqueta}: omitido (destino '{destino}', solo CXP va a Google Sheets)")
        return {"ok": True, "msg": "No se envía a Google Sheets (no es CXP)", "aviso": "", "etiqueta": etiqueta, "omitido": True}
    try:
        resultado = asyncio.run(enviar_fila_webhook_async(fila, etiqueta, adjuntos))
    except Exception:
        resultado = _post_webhook_sync(fila, etiqueta, adjuntos)
    try: st.session_state['webhook_resultado'] = resultado
    except Exception: pass
    return resultado

def _cc_hoja(cc):
    """Siigo entrega '1000-1 - 1000-1 / GCP'; la hoja usa '1000-1 / GCP'. Sin centro de costo -> vacío."""
    t = str(cc or "").strip()
    if t.startswith("--"): return ""
    if " - " in t:
        cola = t.split(" - ", 1)[1]
        if "/" in cola: return cola.strip()
    return t

def armar_adjuntos_webhook(hist_rec, tenant, solo_causacion=False):
    """Archivos que viajan a la carpeta de Drive: factura/cuenta de cobro (PDF y XML) y comprobante de causación."""
    adj = []
    tipo = hist_rec.get("tipo", "FC")
    base = re.sub(r'[\\/:*?"<>|#%]', '', f"{hist_rec.get('proveedor', '')}_{hist_rec.get('id_doc_prov', '')}").strip()[:80] or "documento"
    pref = "Factura" if tipo == "FC" else "CuentaCobro"
    if not solo_causacion:
        try:
            d = json.loads(hist_rec.get("data_json") or "{}")
            if d.get("pdf_b64"): adj.append({"nombre": f"{pref}_{base}.pdf", "mime": "application/pdf", "base64": d["pdf_b64"]})
            if d.get("xml_b64"): adj.append({"nombre": f"{pref}_{base}.xml", "mime": "text/xml", "base64": d["xml_b64"]})
        except Exception as e: print(f"[Webhook Sheets] adjuntos originales: {e}")
    try:
        pdf_c = generar_comprobante_pdf(hist_rec, tenant.get("razon_social", ""), tenant.get("nit", ""))
        if pdf_c: adj.append({"nombre": f"Causacion_{base}.pdf", "mime": "application/pdf", "base64": base64.b64encode(pdf_c).decode("utf-8")})
    except Exception as e: print(f"[Webhook Sheets] comprobante de causación: {e}")
    return adj

def construir_fila_webhook(origen, tenant, usuario, tipo, doc_ref, num_siigo, proveedor, nit,
                           fecha, fecha_venc, moneda, trm, cc, concepto, subtotal, iva, retenciones,
                           total_iva, total_pagar, forma_pago, clasif, estado,
                           fecha_pago="", obs="", fecha_prop=""):
    empresa = (tenant.get("razon_social", "") or "").split(" ")[0]  # "DAVINCI"
    est = str(estado or "").strip().lower()
    if origen == "Tesorería": banco = forma_pago or ""
    elif origen == "Tarjeta": banco = "Tarjeta de Crédito"
    else: banco = ""
    valor_pagado = float(total_pagar or 0) if est == "pagado" else ""

    fila = [
        empresa,                     # A  Empresa
        str(fecha),                  # B  Fecha Recibido
        str(fecha_venc),             # C  Fecha Vto
        str(nit),                    # D  Nit
        proveedor,                   # E  Proveedores
        str(doc_ref),                # F  No. documento
        concepto,                    # G  Concepto
        _cc_hoja(cc),                # H  Centro de Costo
        float(total_iva or 0),       # I  Valor con Iva
        clasif or "",                # J  Clasificacion
        estado or "",                # K  Estado
        "",                          # L  (fórmula)
        "",                          # M  (fórmula)
        "",                          # N  (fórmula)
        "",                          # O  Link Carpeta (lo pone el script)
        "",                          # P  separador
        "",                          # Q  Recibido Contabilidad (manual)
        float(total_pagar or 0),     # R  Valor a Pagar
        "",                          # S  separador
        str(fecha_prop or ""),       # T  Fecha propuesta
        obs or "",                   # U  Observación
        "",                          # V  separador
        str(fecha_pago or ""),       # W  Fecha pago
        valor_pagado,                # X  Valor Pagado
        banco,                       # Y  Banco Girador
        "",                          # Z  Soporte de pago (manual)
    ]
    return fila

def fecha_valida_recepcion(fecha_str):
    """True solo si la fecha (YYYY-MM-DD) es >= 2026-09-01."""
    try:
        return datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d") >= datetime.strptime(FECHA_MINIMA_RECEPCION, "%Y-%m-%d")
    except Exception:
        return False

# ==========================================
# TEMA CLARO FORZADO (evita que el modo oscuro del navegador rompa el diseño)
# ==========================================
_TEMA_TOML = """[theme]
base = "light"
primaryColor = "#2563eb"
backgroundColor = "#f5f7fb"
secondaryBackgroundColor = "#eef2f7"
textColor = "#0f172a"
font = "sans serif"
"""

def _asegurar_tema_claro():
    """Crea/actualiza .streamlit/config.toml con el tema claro. Devuelve True si cambió algo (requiere reiniciar Streamlit una vez)."""
    try:
        carpeta = os.path.join(os.getcwd(), ".streamlit")
        ruta = os.path.join(carpeta, "config.toml")
        if not os.path.exists(ruta):
            os.makedirs(carpeta, exist_ok=True)
            with open(ruta, "w", encoding="utf-8") as f: f.write(_TEMA_TOML)
            return True
        with open(ruta, "r", encoding="utf-8") as f: actual = f.read()
        if "[theme]" not in actual:
            with open(ruta, "a", encoding="utf-8") as f: f.write("\n" + _TEMA_TOML)
            return True
    except Exception:
        pass
    return False

_TEMA_NUEVO = _asegurar_tema_claro()

def fecha_en_rango(fecha_str, usar_rango, desde, hasta):
    """True si la fecha (YYYY-MM-DD) está dentro de [desde, hasta]. Sin filtro, o con fecha ilegible, el documento pasa (nunca se pierde por eso)."""
    if not usar_rango: return True
    try: f = datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d").date()
    except Exception: return True
    return desde <= f <= hasta

def _campos_doc(item, tipo):
    """(doc_ref, nit, fecha, proveedor, total) de una factura FC o un documento soporte DS."""
    if tipo == "FC":
        r = item["Resumen"]
        return r["ID"], r["NIT"], r.get("Fecha", ""), r["Proveedor"], r.get("Total", 0)
    return item["documento_ref"], item["nit"], item.get("fecha", ""), item["proveedor"], item.get("monto_origen", 0)

def guardar_lote_recepcion(tenant_nit, items, tipo, usar_rango, desde, hasta):
    """Guarda como 'Pendiente' lo nuevo y en rango. Lo que cae fuera del rango NO se descarta: se devuelve en 'fuera'."""
    res = {"leidos": 0, "added": 0, "skipped": [], "fuera": []}
    for item in items:
        if not item: continue
        res["leidos"] += 1
        doc_ref, nit_prov, fecha, prov, _ = _campos_doc(item, tipo)
        if not fecha_en_rango(fecha, usar_rango, desde, hasta):
            res["fuera"].append(item); continue
        is_proc, razon = db_is_doc_already_processed(tenant_nit, doc_ref, tipo, nit_prov)
        if not is_proc:
            db_save_doc(tenant_nit, doc_ref, tipo, "Pendiente", item, nit_prov); res["added"] += 1
        else: res["skipped"].append(f"{tipo}-{doc_ref} ({prov}): {razon}")
    return res

def mostrar_resultado_recepcion(tipo):
    """Resumen de la última carga + documentos retenidos por estar fuera del rango (con opción de cargarlos igual)."""
    k_res, k_fuera = f"result_upload_{tipo.lower()}", f"{tipo.lower()}_fuera_rango"
    if k_res in st.session_state:
        res = st.session_state.pop(k_res)
        partes = [f"📬 Leídos: **{res.get('leidos', 0)}**", f"✅ Nuevos: **{res['added']}**",
                  f"♻️ Ya registrados: **{len(res['skipped'])}**", f"📅 Fuera de rango: **{res.get('n_fuera', 0)}**"]
        if res.get("origen"): partes.insert(0, f"Origen: **{res['origen']}**")
        (st.success if res["added"] > 0 else st.warning)("  ·  ".join(partes))
        if res["skipped"]: st.warning("⚠️ **Ya estaban registrados:**\n" + "\n".join([f"* {i}" for i in res["skipped"]]))
    fuera = st.session_state.get(k_fuera) or []
    if fuera:
        with st.expander(f"📅 {len(fuera)} documento(s) fuera del rango de fechas: retenidos, no se perdieron", expanded=True):
            filas = []
            for it in fuera:
                ref, nit, fecha, prov, total = _campos_doc(it, tipo)
                filas.append({"Fecha": fecha, "Documento": ref, "NIT": nit, "Proveedor": prov, "Total": f"{float(total or 0):,.0f}"})
            st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
            b1, b2 = st.columns(2)
            if b1.button("➕ Cargarlos de todas formas", key=f"btn_{tipo}_fuera_cargar", type="primary", use_container_width=True):
                r2 = guardar_lote_recepcion(curr_tenant_nit, fuera, tipo, False, None, None)
                r2["origen"], r2["n_fuera"] = "Retenidos", 0
                st.session_state[k_fuera] = []; st.session_state[k_res] = r2; st.rerun()
            if b2.button("🗑️ Descartarlos", key=f"btn_{tipo}_fuera_descartar", use_container_width=True):
                st.session_state[k_fuera] = []; st.rerun()

def retener_fuera_de_rango(tipo, nuevos):
    """Suma a la lista de retenidos (sin duplicar) lo que quedó fuera del rango."""
    k = f"{tipo.lower()}_fuera_rango"
    actuales = {(_campos_doc(x, tipo)[0], re.sub(r'\D', '', str(_campos_doc(x, tipo)[1]))): x for x in (st.session_state.get(k) or [])}
    for x in nuevos: actuales[(_campos_doc(x, tipo)[0], re.sub(r'\D', '', str(_campos_doc(x, tipo)[1])))] = x
    st.session_state[k] = list(actuales.values())

# ==========================================
# 1. CONFIGURACIÓN Y ESTILOS SAAS
# ==========================================
st.set_page_config(page_title="AutoCount.ai - Conector Siigo", page_icon="⚡", layout="wide", initial_sidebar_state="expanded")
if _TEMA_NUEVO: st.session_state['tema_aviso'] = True

LOGO_URL = "https://start.docuware.com/hubfs/AI%20main%20image.jpg"

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    /* Reset & General Setup */
    .block-container { padding-top: 3.2rem !important; padding-bottom: 1rem !important; max-width: 96%; }
    html, body, [class*="css"] { font-size: 0.75rem !important; font-family: 'Inter', -apple-system, sans-serif; }
    
    /* Top Bar Styling */
    .top-bar-container { background: linear-gradient(90deg, #1e293b 0%, #0f172a 100%); color: white; padding: 12px 24px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; box-shadow: 0 4px 10px -2px rgba(0, 0, 0, 0.2); border: 1px solid #334155; }
    .top-bar-logo { display: flex; align-items: center; gap: 12px; }
    .top-bar-logo img { height: 38px; border-radius: 6px; background: white; padding: 2px;}
    .top-bar-title { font-size: 1.35rem; font-weight: 800; letter-spacing: -0.5px; }
    .top-bar-badge { background-color: #475569; color: #f8fafc; padding: 3px 10px; border-radius: 12px; font-size: 0.65rem; font-weight: 700; margin-left: 8px; border: 1px solid #64748b;}
    .top-bar-user { display: flex; align-items: center; gap: 15px; font-size: 0.8rem; color: #cbd5e1; }
    .top-bar-user b { color: #f8fafc; font-size: 0.85rem;}
    .top-bar-user-badge { background-color: #38bdf8; color: #0f172a; padding: 3px 12px; border-radius: 12px; font-weight: 800; font-size: 0.7rem; box-shadow: 0 2px 4px rgba(56,189,248,0.2);}
    
    /* Sidebar Styling (Light Theme) */
    [data-testid="stSidebar"] { background-color: #f8fafc !important; border-right: 1px solid #e2e8f0; padding-top: 1rem; }
    [data-testid="stSidebarNav"] { display: none; }
    .sidebar-title { font-size: 0.75rem; font-weight: 800; color: #475569; margin-top: 20px; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.8px;}
    div[data-testid="stRadio"] > label { display: none; }
    div[data-testid="stRadio"] > div { gap: 4px; }
    div[data-testid="stRadio"] > div > label { background-color: transparent; padding: 8px 12px; border-radius: 6px; transition: all 0.2s ease; cursor: pointer; }
    div[data-testid="stRadio"] > div > label:hover { background-color: #e2e8f0; }
    div[data-testid="stRadio"] > div > label[data-checked="true"] { background-color: #0f172a; color: white !important; font-weight: bold; }
    div[data-testid="stRadio"] > div > label[data-checked="true"] p { color: white !important; font-weight: bold; }
    
    /* Input & Button Styling */
    .stTextInput label, .stSelectbox label, .stNumberInput label { font-size: 0.7rem !important; font-weight: 700; color: #334155; margin-bottom: -4px !important; }
    .stTextInput input, .stSelectbox select, .stNumberInput input { font-size: 0.75rem !important; padding: 4px 8px !important; height: 32px !important; border-radius: 6px !important; border: 1px solid #cbd5e1 !important;}
    .stButton>button { font-size: 0.75rem !important; font-weight: bold; padding: 0.4rem 1rem !important; border-radius: 6px; transition: all 0.2s ease;}
    .stButton>button[kind="primary"] { background-color: #ef4444; color: white; border: none; }
    .stButton>button[kind="primary"]:hover { background-color: #dc2626; box-shadow: 0 4px 6px -1px rgba(239, 68, 68, 0.3); }
    
    /* Tables & Badges */
    .siigo-table-header { background-color: #f8fafc; padding: 8px 12px; font-weight: 800; font-size: 0.7rem; color: #475569; border-radius: 6px; margin-bottom: 6px; border: 1px solid #e2e8f0; text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center;}
    .badge-ok { background-color: #dcfce7; color: #065f46; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-size: 0.7rem; border: 1px solid #34d399; }
    .badge-warn { background-color: #fef08a; color: #9a3412; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-size: 0.7rem; border: 1px solid #fde047; }
    .badge-danger { background-color: #fee2e2; color: #991b1b; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-size: 0.7rem; border: 1px solid #fca5a5; }
    .badge-siigo { background-color: #e0f2fe; color: #0369a1; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem; border: 1px solid #7dd3fc;}
    .badge-caja { background-color: #f3e8ff; color: #7e22ce; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-size: 0.78rem; border: 1px solid #d8b4fe;}

    /* ===================== TEMA CRM ===================== */
    .stApp { background-color: #f5f7fb; }
    #MainMenu, footer, .stDeployButton, [data-testid="stAppDeployButton"], [data-testid="stMainMenu"], [data-testid="stDecoration"] { display: none !important; }
    header[data-testid="stHeader"] { background: transparent !important; }
    /* Botón para abrir el menú lateral: siempre visible y con buen contraste (no se oculta la barra superior de Streamlit) */
    [data-testid="stSidebarCollapsedControl"] button, [data-testid="stExpandSidebarButton"], [data-testid="collapsedControl"] button { background: #0f172a !important; border-radius: 10px !important; box-shadow: 0 4px 10px -2px rgba(15,23,42,.35) !important; }
    [data-testid="stSidebarCollapsedControl"] *, [data-testid="stExpandSidebarButton"] *, [data-testid="collapsedControl"] * { color: #ffffff !important; fill: #ffffff !important; }
    .page-title { font-size: 1.3rem; font-weight: 800; color: #0f172a; letter-spacing: -0.4px; padding: 2px 0 10px 0; margin-bottom: 8px; border-bottom: 2px solid #e2e8f0; }
    .block-container h4, .block-container h5 { font-weight: 800; color: #1e293b; letter-spacing: -0.2px; }

    /* Barra superior */
    .top-bar-title small { font-size: 0.65rem; font-weight: 700; background: #475569; color: #f8fafc; padding: 2px 9px; border-radius: 12px; margin-left: 8px; border: 1px solid #64748b; vertical-align: middle; }
    .crumb { font-size: 0.7rem; color: #94a3b8; margin-top: 3px; font-weight: 500; }
    .avatar { width: 34px; height: 34px; min-width: 34px; border-radius: 50%; background: linear-gradient(135deg, #38bdf8, #6366f1); color: #fff; display: inline-flex; align-items: center; justify-content: center; font-weight: 800; font-size: 0.8rem; }
    .chip { background: #1e293b; border: 1px solid #334155; color: #cbd5e1; padding: 3px 10px; border-radius: 999px; font-size: 0.65rem; font-weight: 700; white-space: nowrap; }
    .chip-ok { background: rgba(34,197,94,.15); border-color: #22c55e; color: #86efac; }
    .chip-bad { background: rgba(239,68,68,.15); border-color: #ef4444; color: #fca5a5; }
    .top-bar-who { display: flex; align-items: center; gap: 10px; }
    .top-bar-who b { color: #f8fafc; font-size: 0.8rem; display: block; line-height: 1.1; }

    /* Sidebar */
    .sb-brand { display: flex; align-items: center; gap: 10px; padding: 2px 4px 12px 4px; border-bottom: 1px solid #e2e8f0; margin-bottom: 12px; }
    .sb-brand img { height: 30px; border-radius: 6px; }
    .sb-brand-name { font-size: 1.1rem; font-weight: 800; color: #0f172a; letter-spacing: -0.5px; }
    .sb-user { display: flex; align-items: center; gap: 10px; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 9px 10px; margin-bottom: 10px; box-shadow: 0 1px 2px rgba(15,23,42,.05); }
    .sb-user-name { font-weight: 700; color: #0f172a; font-size: 0.78rem; line-height: 1.15; }
    .sb-user-role { font-size: 0.65rem; color: #64748b; font-weight: 600; }
    div[data-testid="stRadio"] label:has(input:checked) { background-color: #0f172a !important; }
    div[data-testid="stRadio"] label:has(input:checked) p { color: #ffffff !important; font-weight: 700 !important; }

    /* Botones */
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button { border-radius: 8px; }
    .stButton>button[kind="primary"], button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"] { background: linear-gradient(135deg, #2563eb, #1d4ed8) !important; color: #ffffff !important; border: none !important; }
    .stButton>button[kind="primary"]:hover, button[data-testid="stBaseButton-primary"]:hover, button[data-testid="stBaseButton-primaryFormSubmit"]:hover { box-shadow: 0 6px 14px -4px rgba(37,99,235,.55) !important; filter: brightness(1.06); }

    /* Tarjetas KPI */
    [data-testid="stMetric"] { background: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid #38bdf8; border-radius: 10px; padding: 10px 14px; box-shadow: 0 1px 2px rgba(15,23,42,.05); }
    [data-testid="stMetricLabel"] p { color: #64748b !important; font-weight: 700 !important; text-transform: uppercase; letter-spacing: .4px; font-size: .65rem !important; }
    [data-testid="stMetricValue"] { color: #0f172a; font-weight: 800; font-size: 1.25rem !important; line-height: 1.25; }
    [data-testid="stMetricValue"] div { overflow: visible !important; text-overflow: clip !important; white-space: nowrap; }

    /* Pestañas */
    .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid #e2e8f0; }
    .stTabs [data-baseweb="tab"] { height: 38px; padding: 0 14px; border-radius: 8px 8px 0 0; font-weight: 700; }
    .stTabs [aria-selected="true"] { color: #0f172a; background: #ffffff; }

    /* Dashboard */
    .pipe { display: flex; gap: 14px; margin: 4px 0 14px 0; }
    .pipe-step { flex: 1; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 12px 14px; position: relative; box-shadow: 0 1px 2px rgba(15,23,42,.05); }
    .pipe-step .n { font-size: 1.5rem; font-weight: 800; color: #0f172a; line-height: 1.15; }
    .pipe-step .l { font-size: .65rem; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: .5px; }
    .pipe-step .d { font-size: .65rem; color: #94a3b8; margin-top: 2px; }
    .pipe-step::after { content: '›'; position: absolute; right: -11px; top: 50%; transform: translateY(-55%); color: #94a3b8; font-size: 1.5rem; z-index: 2; }
    .pipe-step:last-child::after { content: ''; }
    .pipe-1 { border-top: 3px solid #f59e0b; } .pipe-2 { border-top: 3px solid #3b82f6; } .pipe-3 { border-top: 3px solid #8b5cf6; } .pipe-4 { border-top: 3px solid #22c55e; }
    .panel-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(15,23,42,.05); margin-bottom: 12px; }
    .panel-card h5 { margin: 0 0 10px 0; font-size: .8rem; font-weight: 800; color: #0f172a; }
    .bar-row { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
    .bar-label { width: 34%; font-size: .72rem; color: #475569; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .bar-track { flex: 1; background: #f1f5f9; border-radius: 999px; height: 10px; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 999px; }
    .bar-val { width: 96px; text-align: right; font-size: .72rem; font-weight: 700; color: #0f172a; }

    /* ============ LEGIBILIDAD (aunque el navegador esté en modo oscuro) ============ */
    .stMarkdown p, .stMarkdown li, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4, .stMarkdown h5, .stMarkdown h6 { color: #0f172a; }
    [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p, .stCaption { color: #64748b !important; }
    [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { color: #334155 !important; }
    .stTabs [data-baseweb="tab"] p, .stTabs [data-baseweb="tab"] { color: #475569 !important; }
    .stTabs [aria-selected="true"] p, .stTabs [aria-selected="true"] { color: #0f172a !important; }
    .stTabs [data-baseweb="tab-highlight"] { background-color: #2563eb !important; height: 3px; }
    [data-testid="stExpander"] details { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }
    [data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span { color: #0f172a !important; }
    div[data-baseweb="select"] > div, div[data-baseweb="input"] > div, div[data-baseweb="textarea"] { background-color: #ffffff !important; border-color: #cbd5e1 !important; }
    div[data-baseweb="select"] span, div[data-baseweb="select"] div, input, textarea { color: #0f172a !important; }
    .stButton>button:not([kind="primary"]), button[data-testid="stBaseButton-secondary"], .stDownloadButton>button, button[data-testid="stBaseButton-secondaryFormSubmit"] { background: #ffffff !important; color: #0f172a !important; border: 1px solid #cbd5e1 !important; }
    .stButton>button:not([kind="primary"]):hover, button[data-testid="stBaseButton-secondary"]:hover, .stDownloadButton>button:hover { border-color: #2563eb !important; color: #1d4ed8 !important; background: #eff6ff !important; }
    [data-testid="stFileUploaderDropzone"] { background: #ffffff !important; border: 1.5px dashed #94a3b8 !important; border-radius: 12px !important; }
    [data-testid="stFileUploaderDropzone"] * { color: #475569 !important; }
    [data-testid="stFileUploaderDropzone"] button { background: #ffffff !important; border: 1px solid #cbd5e1 !important; color: #0f172a !important; }
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] code, [data-testid="stSidebar"] code { background: #e0f2fe; color: #0369a1; padding: 1px 6px; border-radius: 6px; font-weight: 700; }
    ::-webkit-scrollbar { width: 8px; height: 8px; } ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 8px; } ::-webkit-scrollbar-track { background: transparent; }

    /* ============ NAVEGACIÓN LATERAL PRO ============ */
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label > div:first-child:not(:has([data-testid="stMarkdownContainer"])) { display: none !important; }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] { gap: 3px; }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label { width: 100%; padding: 10px 12px !important; border-radius: 10px; border-left: 3px solid transparent; transition: background .15s ease, border-color .15s ease; }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label p { color: #334155 !important; font-weight: 600; font-size: .78rem; }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label:hover { background-color: #e8eef7; }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) { background: linear-gradient(90deg, #0f172a, #1e293b) !important; border-left: 3px solid #38bdf8; box-shadow: 0 6px 12px -6px rgba(15,23,42,.45); }
    [data-testid="stSidebar"] div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) p { color: #ffffff !important; font-weight: 700; }
    div[data-testid="stRadio"] label:has(input:checked) p { color: #ffffff !important; }
    .sb-foot { margin-top: 14px; padding-top: 10px; border-top: 1px solid #e2e8f0; font-size: .62rem; color: #94a3b8; text-align: center; line-height: 1.5; }
    .chip-lite { display: inline-block; background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; padding: 3px 11px; border-radius: 999px; font-size: .68rem; font-weight: 700; margin: 0 3px; }
    .login-box { max-width: 400px; margin: 60px auto; padding: 30px; border: 1px solid #e2e8f0; border-radius: 12px; background-color: #ffffff; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
</style>
""", unsafe_allow_html=True)

DEFAULT_PUC = [
    "51355001 - Servicios Técnicos Exterior", "51353501 - Comisiones y Honorarios", "51352001 - Procesamiento de Datos y Software",
    "51351501 - Asistencia Técnica", "51350501 - Aseo y Vigilancia", "51354001 - Teléfono y Comunicaciones",
    "51357001 - Asesoría Jurídica y Financiera", "51359501 - Otros Servicios Diversos", "23651501 - Retención en la Fuente - Honorarios / Servicios"
]

OPCIONES_CLASIFICACION = ["Proveedor", "Nómina de Servicios", "Reembolso", "Ser.Publicos", "Viajes"]

# ==========================================
# 2. FUNCIONES BASE Y DE UTILIDAD
# ==========================================
def obtener_mes_str(fecha_str):
    if pd.isna(fecha_str) or not fecha_str or len(str(fecha_str)) < 7: return "Sin Fecha"
    meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    try:
        partes = str(fecha_str).split("-")
        y, m = partes[0], int(partes[1])
        return f"{meses[m-1]} {y}"
    except: return "Sin Fecha"

def render_month_header(current_month_str, new_date_str):
    new_m = obtener_mes_str(new_date_str)
    if new_m != current_month_str:
        st.markdown(f"<div style='background-color:#f8fafc; padding:8px 12px; border-radius:6px; margin-top:15px; margin-bottom:10px; font-weight:800; color:#334155; border-left: 4px solid #38bdf8;'>📅 {new_m}</div>", unsafe_allow_html=True)
        return new_m
    return current_month_str

def safe_float(val):
    if pd.isna(val) or val == "": return 0.0
    if isinstance(val, (int, float)): return float(val)
    val_str = str(val).replace("$", "").replace(" ", "").replace(",", "").strip()
    try: return float(val_str)
    except Exception: return 0.0

def safe_b64decode(data):
    if not data: return None
    try:
        data_str = data if isinstance(data, str) else data.decode('utf-8')
        data_str = re.sub(r'\s+', '', data_str)
        pad = len(data_str) % 4
        if pad > 0: data_str += '=' * (4 - pad)
        return base64.b64decode(data_str)
    except Exception: return None

def buscar_indice_tercero(nombre_prov, nit_prov, terceros_lista):
    if not terceros_lista: return -1
    nit_clean = re.sub(r'\D', '', str(nit_prov or ''))
    if nit_clean:
        for idx, item in enumerate(terceros_lista):
            if re.sub(r'\D', '', item.split(" - ")[0].strip()) == nit_clean: return idx
    p_clean = (nombre_prov or "").lower().strip()
    if not p_clean: return -1
    best_idx, best_score = -1, 0.65
    for idx, item in enumerate(terceros_lista):
        score = difflib.SequenceMatcher(None, p_clean, item.lower()).ratio()
        if score > best_score: best_score, best_idx = score, idx
    return best_idx

def buscar_indice_iva(iva_pct_xml, list_iva):
    if not iva_pct_xml or float(iva_pct_xml) == 0: return 0
    for idx, imp in enumerate(list_iva):
        if abs(float(imp.get("porcentaje", 0)) - float(iva_pct_xml)) < 0.5: return idx
    return 0

class PredictiveEngine:
    def __init__(self, masters: Dict[str, Any], history: List[dict] = None): self.masters = masters
    def predict_mapping(self, provider_name: str, nit: str, descripcion: str) -> dict:
        clean_name = (provider_name or "").lower().strip()
        cc_def = self.masters.get("centros_costo", [{}])[0].get("id") if self.masters.get("centros_costo") else None
        if any(k in clean_name for k in ["google", "docusign", "monday"]): return {"puc_code": "51355001", "cost_center_id": cc_def}
        elif any(k in clean_name for k in ["bernal", "william", "factotal", "ft capital"]): return {"puc_code": "51353501", "cost_center_id": cc_def}
        return {"puc_code": "51353501", "cost_center_id": cc_def}

@st.cache_data(ttl=3600)
def consultar_trm_oficial_script(fecha_str):
    try:
        url = f"https://www.datos.gov.co/resource/32sa-8pi3.json?$where=vigenciadesde<='{fecha_str[:10]}T23:59:59.000'&$order=vigenciadesde DESC&$limit=1"
        res = requests.get(url, timeout=5)
        if res.status_code == 200 and len(res.json()) > 0: return float(res.json()[0]['valor'])
    except Exception: pass
    return 3995.00

def generar_excel(df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Reporte')
    return output.getvalue()

def calcular_categoria_dias(dias):
    if pd.isna(dias) or dias < 0: return "Vigente"
    if dias <= 30: return "1 a 30"
    if dias <= 60: return "31 a 60"
    if dias <= 90: return "61 a 90"
    return "Mayor a 90"

def hash_password(password, email):
    """Hash PBKDF2 con sal por usuario (el correo). Formato: pbkdf2$<hex>."""
    salt = ("autocount::" + str(email).lower().strip()).encode()
    return "pbkdf2$" + hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt, 120000).hex()

@st.cache_resource(show_spinner=False)
def _registro_intentos():
    return {}

def _segundos_bloqueo(email, max_intentos=5, ventana=600):
    """Segundos que faltan para poder reintentar (0 = puede intentar). 5 fallos en 10 minutos bloquean ese correo."""
    reg, k, ahora_ts = _registro_intentos(), str(email or "").lower().strip(), time.time()
    fallos = [t for t in reg.get(k, []) if ahora_ts - t < ventana]
    reg[k] = fallos
    return int(ventana - (ahora_ts - fallos[0])) if len(fallos) >= max_intentos else 0

def _registrar_fallo(email):
    _registro_intentos().setdefault(str(email or "").lower().strip(), []).append(time.time())

def _limpiar_intentos(email):
    _registro_intentos().pop(str(email or "").lower().strip(), None)

def page_title(texto):
    st.markdown(f"<div class='page-title'>{texto}</div>", unsafe_allow_html=True)

def render_barras_html(titulo, items):
    """Barras horizontales en HTML (orden fijo, sin depender de gráficos)."""
    max_v = max([v for _, v, _ in items], default=0) or 1
    filas = ""
    for label, val, color in items:
        ancho = max(2, int(val / max_v * 100)) if val > 0 else 0
        filas += ("<div class='bar-row'><div class='bar-label' title='" + html.escape(str(label)) + "'>" + html.escape(str(label)) + "</div>"
                  "<div class='bar-track'><div class='bar-fill' style='width:" + str(ancho) + "%; background:" + color + ";'></div></div>"
                  "<div class='bar-val'>&#36;" + f"{val:,.0f}" + "</div></div>")
    if not items: filas = "<div style='color:#94a3b8; font-size:.75rem;'>Sin datos todavía.</div>"
    return "<div class='panel-card'><h5>" + titulo + "</h5>" + filas + "</div>"

def db_dashboard_history(tenant_nit, limit=500):
    """Historial liviano (sin PDFs) para el dashboard."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT doc_ref, tipo, fecha, total, moneda, siigo_id, proveedor, usuario FROM history WHERE tenant_nit=? ORDER BY fecha DESC LIMIT ?", (tenant_nit, limit))
    rows = c.fetchall()
    conn.close()
    return [{"doc_ref": r[0], "tipo": r[1], "fecha": r[2], "total": r[3] or 0, "moneda": r[4], "siigo_id": r[5] or "", "proveedor": r[6] or "", "usuario": r[7] or "N/A"} for r in rows]

def construir_resumen_dashboard(tenant_nit):
    """Solo lectura: arma los indicadores del panel de control."""
    hoy = datetime.now()
    mes_actual = hoy.strftime("%Y-%m")
    fc_pend = db_get_docs(tenant_nit, "FC", "Pendiente")
    ds_pend = db_get_docs(tenant_nit, "DS", "Pendiente")
    fc_apr = db_get_docs(tenant_nit, "FC", "Aprobado")
    ds_apr = [d for d in db_get_docs(tenant_nit, "DS", "Aprobado") if d.get("Clasificacion") != "Caja Menor"]
    caja = len(db_get_docs(tenant_nit, "FC", "Caja Menor")) + len(db_get_docs(tenant_nit, "DS", "Caja Menor"))
    hist = db_dashboard_history(tenant_nit)

    aging = {"Vigente": 0.0, "1 a 30": 0.0, "31 a 60": 0.0, "61 a 90": 0.0, "Mayor a 90": 0.0}
    deuda = programado = pagado_mes = 0.0
    por_prov, proximos = {}, []
    for t in db_get_treasury(tenant_nit):
        valor = float(t.get("total_pagar") or 0)
        estado = t.get("estado")
        if estado == "Pagado":
            if str(t.get("fecha_pago") or "")[:7] == mes_actual: pagado_mes += valor
            continue
        try: dias, fecha_ok = (hoy - datetime.strptime(str(t.get("fecha_vencimiento") or "")[:10], "%Y-%m-%d")).days, True
        except Exception: dias, fecha_ok = -1, False
        aging[calcular_categoria_dias(dias)] += valor
        deuda += valor
        if estado == "Programado": programado += valor
        prov = str(t.get("proveedor") or "Sin proveedor").strip()
        por_prov[prov] = por_prov.get(prov, 0.0) + valor
        if fecha_ok and -7 <= dias <= 0:
            proximos.append({"Vence": str(t.get("fecha_vencimiento"))[:10], "Proveedor": prov[:35], "Doc": t.get("doc_ref"), "Valor a pagar": f"${valor:,.0f}", "Estado": estado})

    ultimas = [{"Fecha": h["fecha"], "Tipo": h["tipo"], "Doc": h["doc_ref"], "Proveedor": h["proveedor"][:35],
                "Total": ("USD $" if h["moneda"] == "USD" else "$") + f"{h['total']:,.0f}", "Usuario": h["usuario"],
                "N° Siigo": h["siigo_id"].split("|||")[0]} for h in hist[:8]]
    return {
        "pend_aprobacion": len(fc_pend) + len(ds_pend), "por_causar": len(fc_apr) + len(ds_apr), "caja": caja,
        "causados_total": len(hist), "causados_mes": sum(1 for h in hist if str(h["fecha"])[:7] == mes_actual),
        "deuda": deuda, "vencido": deuda - aging["Vigente"], "programado": programado, "pagado_mes": pagado_mes,
        "aging": aging, "top_prov": sorted(por_prov.items(), key=lambda x: x[1], reverse=True)[:5],
        "proximos": sorted(proximos, key=lambda x: x["Vence"])[:12], "ultimas": ultimas,
    }

# ==========================================
# 3. BASE DE DATOS LOCAL PERSISTENTE (SQLite)
# ==========================================
# --- Base de datos: si hay DATABASE_URL (secreto) se usa PostgreSQL en la nube (persistente);
# --- si no, se usa el archivo local autocount.db como siempre.
DATABASE_URL = _secret("DATABASE_URL")
USANDO_POSTGRES = bool(DATABASE_URL)
MODO_BD = "PostgreSQL (persistente)" if USANDO_POSTGRES else "archivo local"
_ULTIMO_USO = {}   # id(conexión) -> última vez que se usó (para detectar conexiones dormidas)

def _traducir_sql(sql):
    """Convierte el SQL de SQLite (el que usa toda la app) a PostgreSQL."""
    s = sql
    m = re.search(r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)', s, re.I | re.S)
    if m:
        tabla, cols, vals = m.group(1), [c.strip() for c in m.group(2).split(",")], m.group(3)
        pk = {"tenants": "nit", "users": "email"}.get(tabla.lower(), "id")   # llave primaria de cada tabla
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != pk)
        s = s[:m.start()] + f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES ({vals}) ON CONFLICT ({pk}) DO UPDATE SET {sets}" + s[m.end():]
    if s.lstrip().upper().startswith("CREATE TABLE"): s = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', s)   # REAL de Postgres pierde decimales
    s = re.sub(r'ADD COLUMN(?!\s+IF NOT EXISTS)', 'ADD COLUMN IF NOT EXISTS', s, flags=re.I)
    return s.replace("%", "%%").replace("?", "%s")

def _limpiar_param(x):
    """Valores que SQLite tolera pero PostgreSQL no (NaN, tipos numpy/pandas)."""
    if x is None or x is pd.NA: return None
    try:
        if hasattr(x, "item") and not isinstance(x, (str, bytes)): x = x.item()
    except Exception: pass
    if isinstance(x, float) and math.isnan(x): return None
    return x

def _obtener_conexion_viva(pool):
    """Toma una conexión del pool; si estuvo inactiva más de 2 min comprueba que siga viva (el servidor gratuito se duerme)."""
    for _ in range(14):
        raw = pool.getconn()
        if time.time() - _ULTIMO_USO.get(id(raw), 0) > 120:
            try:
                cur = raw.cursor(); cur.execute("SELECT 1"); cur.close(); raw.rollback()
            except Exception:
                try: pool.putconn(raw, close=True)
                except Exception: pass
                continue
        return raw
    raise RuntimeError("No se pudo abrir una conexión con la base de datos.")

class _PGCursor:
    def __init__(self, conn): self.conn, self.cur = conn, conn._raw.cursor()
    @property
    def connection(self): return self.conn
    def execute(self, sql, params=()):
        sql2, p = _traducir_sql(sql), tuple(_limpiar_param(x) for x in (params or ()))
        try: self.cur.execute(sql2, p)
        except (self.conn._pg.OperationalError, self.conn._pg.InterfaceError):
            self.conn._reconectar(); self.cur = self.conn._raw.cursor(); self.cur.execute(sql2, p)   # conexión caída: 1 reintento
        return self
    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()

class _PGConn:
    """Se comporta como una conexión de sqlite3 (cursor/execute/commit/close) pero sobre un pool de PostgreSQL."""
    def __init__(self, pool):
        import psycopg2
        self._pg, self._pool, self._cerrada = psycopg2, pool, False
        self._raw = _obtener_conexion_viva(pool)
    def cursor(self): return _PGCursor(self)
    def execute(self, sql, params=()): return self.cursor().execute(sql, params)
    def commit(self): self._raw.commit()
    def rollback(self): self._raw.rollback()
    def _reconectar(self):
        try: self._pool.putconn(self._raw, close=True)
        except Exception: pass
        self._raw = _obtener_conexion_viva(self._pool)
    def close(self):
        if self._cerrada: return
        self._cerrada = True
        try: self._raw.rollback()   # descarta lo que no se confirmó (y limpia una transacción abortada)
        except Exception: pass
        _ULTIMO_USO[id(self._raw)] = time.time()
        try: self._pool.putconn(self._raw)
        except Exception: pass
    def __del__(self):
        try: self.close()
        except Exception: pass

@st.cache_resource(show_spinner=False)
def _pool_pg(dsn):
    import psycopg2.pool
    return psycopg2.pool.ThreadedConnectionPool(1, 12, dsn, connect_timeout=20, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)

def get_db_connection():
    if USANDO_POSTGRES: return _PGConn(_pool_pg(DATABASE_URL))
    return sqlite3.connect('autocount.db', check_same_thread=False)

_DDL_TABLAS = [
    'CREATE TABLE IF NOT EXISTS tenants (nit TEXT PRIMARY KEY, razon_social TEXT, siigo_user TEXT, siigo_key TEXT, puc TEXT)',
    'CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, password TEXT, nombre TEXT, tenant_nit TEXT, rol TEXT, activo INTEGER DEFAULT 1)',
    'CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, tenant_nit TEXT, doc_ref TEXT, tipo TEXT, estado TEXT, data TEXT)',
    'CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, tenant_nit TEXT, doc_ref TEXT, tipo TEXT, fecha TEXT, total REAL, moneda TEXT, siigo_id TEXT, proveedor TEXT, nit_proveedor TEXT, pdf_b64 TEXT, data_json TEXT, usuario TEXT)',
    'CREATE TABLE IF NOT EXISTS treasury (id TEXT PRIMARY KEY, tenant_nit TEXT, doc_ref TEXT, proveedor TEXT, nit_proveedor TEXT, fecha_recibido TEXT, fecha_vencimiento TEXT, concepto TEXT, centro_costo TEXT, valor_con_iva REAL, total_pagar REAL, estado TEXT, clasificacion TEXT, fecha_propuesta TEXT, observacion TEXT, fecha_pago TEXT, banco_girador TEXT, raw_data TEXT)',
]

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    for _ddl in _DDL_TABLAS: c.execute(_ddl)
    try: c.execute("ALTER TABLE users ADD COLUMN activo INTEGER DEFAULT 1")   # bases creadas antes de la gestión de usuarios
    except Exception: pass
    try: c.execute('ALTER TABLE treasury ADD COLUMN raw_data TEXT')
    except: pass
    
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        _adm_mail = _secret("ADMIN_EMAIL", "tomas.suta@davinci.tech").lower().strip()
        c.execute("INSERT INTO tenants VALUES (?,?,?,?,?)", ('900557218', 'DAVINCI TECHNOLOGIES SAS', _secret("SIIGO_USER", _adm_mail), _secret("SIIGO_KEY", ""), json.dumps(DEFAULT_PUC)))
        c.execute("INSERT INTO users (email, password, nombre, tenant_nit, rol, activo) VALUES (?,?,?,?,?,?)", (_adm_mail, hash_password(_secret("ADMIN_INITIAL_PASSWORD", "admin"), _adm_mail), 'Tomás Suta', '900557218', 'SuperAdmin', 1))
    conn.commit()
    conn.close()

@st.cache_resource(show_spinner=False)
def _init_db_cached():
    init_db()
    return True

_init_db_cached()

def db_is_doc_already_processed(tenant_nit, doc_ref, tipo, nit_prov):
    conn = get_db_connection()
    c = conn.cursor()
    clean_nit = re.sub(r'\D', '', str(nit_prov))
    doc_id = f"{tenant_nit}_{tipo}_{clean_nit}_{doc_ref}"
    
    c.execute("SELECT COUNT(*) FROM history WHERE id=?", (doc_id,))
    if c.fetchone()[0] > 0:
        conn.close()
        return True, "ya fue causada en Siigo (Histórico)"
        
    c.execute("SELECT estado FROM docs WHERE id=?", (doc_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0] in ['Aprobado', 'Rechazado', 'Pendiente', 'Caja Menor']: return True, f"ya está registrada en estado {row[0]}"
    return False, None

def auto_clean_processed_docs(tenant_nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM docs WHERE tenant_nit=? AND id IN (SELECT d.id FROM docs d JOIN history h ON d.tenant_nit = h.tenant_nit AND d.doc_ref = h.doc_ref AND d.tipo = h.tipo)", (tenant_nit,))
    conn.commit(); conn.close()

def _activo(v):
    """NULL (bases antiguas) cuenta como activo."""
    return v is None or int(v) != 0

def db_auth_user(email, password):
    email = str(email or "").lower().strip()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT email, nombre, tenant_nit, rol, password, activo FROM users WHERE email=?", (email,))
    user = c.fetchone()
    if not user:
        conn.close()
        return None
    guardada = user[4] or ""
    if guardada.startswith("pbkdf2$"):
        ok = hmac.compare_digest(guardada, hash_password(password, email))
    else:
        # Clave antigua en texto plano: se acepta una vez y se migra a hash automáticamente
        ok = hmac.compare_digest(guardada.encode(), str(password).encode())
        if ok and _activo(user[5]):
            c.execute("UPDATE users SET password=? WHERE email=?", (hash_password(password, email), email))
            conn.commit()
    conn.close()
    if ok and _activo(user[5]): return {"email": user[0], "nombre": user[1], "tenant_nit": user[2], "rol": user[3]}
    return None

def db_usuario_desactivado(email, password):
    """True solo si la contraseña es correcta pero el usuario está desactivado (para mostrar un mensaje claro sin revelar nada más)."""
    email = str(email or "").lower().strip()
    conn = get_db_connection(); c = conn.cursor()
    try: c.execute("SELECT password, activo FROM users WHERE email=?", (email,)); r = c.fetchone()
    finally: conn.close()
    if not r or _activo(r[1]): return False
    g = r[0] or ""
    return hmac.compare_digest(g, hash_password(password, email)) if g.startswith("pbkdf2$") else hmac.compare_digest(g.encode(), str(password).encode())

def db_usuario_vigente(email):
    """Datos actuales del usuario si existe y está activo; None si fue desactivado o eliminado."""
    conn = get_db_connection(); c = conn.cursor()
    try: c.execute("SELECT nombre, rol, tenant_nit, activo FROM users WHERE email=?", (str(email or "").lower().strip(),)); r = c.fetchone()
    finally: conn.close()
    if not r or not _activo(r[3]): return None
    return {"nombre": r[0], "rol": r[1], "tenant_nit": r[2]}

# ---------- Gestión de usuarios (las reglas viven aquí, no solo en la pantalla) ----------
ROLES_USUARIO = ["Administrativo", "Auxiliar Administrativo", "Asistente Contable", "Administrador", "SuperAdmin"]

def roles_asignables(rol_actor):
    """Solo un SuperAdmin puede crear o asignar el rol SuperAdmin."""
    return list(ROLES_USUARIO) if rol_actor == "SuperAdmin" else ROLES_USUARIO[:4]

def puede_gestionar_usuario(rol_actor, rol_objetivo):
    """SuperAdmin gestiona a todos; Administrador gestiona a su empresa pero nunca a un SuperAdmin."""
    return rol_actor == "SuperAdmin" or (rol_actor == "Administrador" and rol_objetivo != "SuperAdmin")

def generar_password_temporal(n=10):
    import secrets, string
    alfabeto = "".join(ch for ch in string.ascii_letters + string.digits if ch not in "O0Il1")   # sin caracteres que se confunden
    return "".join(secrets.choice(alfabeto) for _ in range(n))

def db_listar_usuarios(tenant_nit):
    conn = get_db_connection(); c = conn.cursor()
    try: c.execute("SELECT email, nombre, rol, activo FROM users WHERE tenant_nit=?", (tenant_nit,)); filas = c.fetchall()
    finally: conn.close()
    out = [{"email": f[0], "nombre": f[1] or "", "rol": f[2], "activo": _activo(f[3])} for f in filas]
    return sorted(out, key=lambda u: (not u["activo"], u["nombre"].lower(), u["email"]))

def _usuario_objetivo(cur, email, tenant_nit):
    cur.execute("SELECT email, nombre, rol, activo FROM users WHERE email=? AND tenant_nit=?", (str(email or "").lower().strip(), tenant_nit))
    r = cur.fetchone()
    if not r: raise ValueError("El usuario no existe en esta empresa.")
    return {"email": r[0], "nombre": r[1], "rol": r[2], "activo": _activo(r[3])}

def _otros_superadmins_activos(cur, excluir_email):
    cur.execute("SELECT email, activo FROM users WHERE rol=?", ("SuperAdmin",))
    return sum(1 for e, a in cur.fetchall() if e != excluir_email and _activo(a))

def db_actualizar_usuario(actor, email, tenant_nit, nombre, rol, activo):
    """Cambia nombre, rol y estado. Lanza ValueError con el motivo si no está permitido."""
    nombre = str(nombre or "").strip()
    if not nombre: raise ValueError("El nombre no puede estar vacío.")
    conn = get_db_connection(); c = conn.cursor()
    try:
        obj = _usuario_objetivo(c, email, tenant_nit)
        if not puede_gestionar_usuario(actor.get("rol"), obj["rol"]): raise ValueError("No tienes permiso para modificar a este usuario.")
        if rol not in roles_asignables(actor.get("rol")): raise ValueError("No puedes asignar ese rol.")
        if obj["email"] == str(actor.get("email", "")).lower().strip() and (rol != obj["rol"] or not activo): raise ValueError("No puedes cambiar tu propio rol ni desactivarte.")
        if obj["rol"] == "SuperAdmin" and obj["activo"] and (rol != "SuperAdmin" or not activo) and _otros_superadmins_activos(c, obj["email"]) == 0:
            raise ValueError("Debe quedar al menos un SuperAdmin activo.")
        c.execute("UPDATE users SET nombre=?, rol=?, activo=? WHERE email=? AND tenant_nit=?", (nombre, rol, 1 if activo else 0, obj["email"], tenant_nit))
        conn.commit()
    finally: conn.close()

def db_restablecer_password(actor, email, tenant_nit, nueva):
    if len(str(nueva or "")) < 6: raise ValueError("La contraseña debe tener al menos 6 caracteres.")
    conn = get_db_connection(); c = conn.cursor()
    try:
        obj = _usuario_objetivo(c, email, tenant_nit)
        if not puede_gestionar_usuario(actor.get("rol"), obj["rol"]): raise ValueError("No tienes permiso para cambiar la contraseña de este usuario.")
        c.execute("UPDATE users SET password=? WHERE email=? AND tenant_nit=?", (hash_password(nueva, obj["email"]), obj["email"], tenant_nit))
        conn.commit()
    finally: conn.close()

def db_eliminar_usuario(actor, email, tenant_nit):
    conn = get_db_connection(); c = conn.cursor()
    try:
        obj = _usuario_objetivo(c, email, tenant_nit)
        if not puede_gestionar_usuario(actor.get("rol"), obj["rol"]): raise ValueError("No tienes permiso para eliminar a este usuario.")
        if obj["email"] == str(actor.get("email", "")).lower().strip(): raise ValueError("No puedes eliminarte a ti mismo.")
        if obj["rol"] == "SuperAdmin" and obj["activo"] and _otros_superadmins_activos(c, obj["email"]) == 0: raise ValueError("Debe quedar al menos un SuperAdmin activo.")
        c.execute("DELETE FROM users WHERE email=? AND tenant_nit=?", (obj["email"], tenant_nit))
        conn.commit()
    finally: conn.close()

def db_get_tenant(nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT nit, razon_social, siigo_user, siigo_key, puc FROM tenants WHERE nit=?", (nit,))
    row = c.fetchone()
    conn.close()
    if row: return {"nit": row[0], "razon_social": row[1], "siigo_user": row[2], "siigo_key": row[3], "puc": json.loads(row[4] or '[]')}
    return None

def db_save_doc(tenant_nit, doc_ref, tipo, estado, data_dict, nit_prov):
    conn = get_db_connection()
    c = conn.cursor()
    clean_nit = re.sub(r'\D', '', str(nit_prov))
    doc_id = f"{tenant_nit}_{tipo}_{clean_nit}_{doc_ref}"
    c.execute("INSERT OR REPLACE INTO docs (id, tenant_nit, doc_ref, tipo, estado, data) VALUES (?,?,?,?,?,?)", (doc_id, tenant_nit, doc_ref, tipo, estado, json.dumps(data_dict)))
    conn.commit(); conn.close()

def db_delete_doc(tenant_nit, doc_ref, tipo, nit_prov):
    conn = get_db_connection()
    c = conn.cursor()
    clean_nit = re.sub(r'\D', '', str(nit_prov))
    c.execute("DELETE FROM docs WHERE id=?", (f"{tenant_nit}_{tipo}_{clean_nit}_{doc_ref}",))
    conn.commit(); conn.close()

def db_get_docs(tenant_nit, tipo, estado=None):
    conn = get_db_connection()
    c = conn.cursor()
    if estado: c.execute("SELECT data FROM docs WHERE tenant_nit=? AND tipo=? AND estado=?", (tenant_nit, tipo, estado))
    else: c.execute("SELECT data FROM docs WHERE tenant_nit=? AND tipo=?", (tenant_nit, tipo))
    rows = c.fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]

def db_save_history(tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, prov, nit_prov, pdf_b64, data_json="{}", usuario=""):
    conn = get_db_connection()
    c = conn.cursor()
    clean_nit = re.sub(r'\D', '', str(nit_prov))
    hist_id = f"{tenant_nit}_{tipo}_{clean_nit}_{doc_ref}"
    try:
        _d = json.loads(data_json or "{}")
        _quitados = [_d.pop(k, None) for k in ("pdf_b64", "xml_b64")]   # se quitan las dos claves (sin cortocircuito)
        if any(x is not None for x in _quitados): data_json = json.dumps(_d)
    except Exception: pass
    c.execute("INSERT OR REPLACE INTO history (id, tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, proveedor, nit_proveedor, pdf_b64, data_json, usuario) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (hist_id, tenant_nit, doc_ref, tipo, fecha, total, moneda, siigo_id, prov, nit_prov, pdf_b64, data_json, usuario))
    c.execute("DELETE FROM docs WHERE id=?", (hist_id,))
    conn.commit(); conn.close()

def db_get_history(tenant_nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT doc_ref, tipo, fecha, total, moneda, siigo_id, proveedor, nit_proveedor, pdf_b64, data_json, usuario FROM history WHERE tenant_nit=? ORDER BY fecha DESC", (tenant_nit,))
    rows = c.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "id_doc_prov": r[0], "tipo": r[1], "fecha": r[2], "total": r[3], "moneda": r[4], 
            "id_siigo_num": r[5], "proveedor": r[6], "nit": r[7], "pdf_original": safe_b64decode(r[8]),
            "data_json": r[9] if r[9] else "{}", "usuario": r[10] if r[10] else "N/A"
        })
    return res

def db_save_treasury(tenant_nit, doc_ref, prov, nit_prov, fecha_recibido, fecha_venc, concepto, cc, val_iva, tot_pagar, estado, clasificacion, fecha_prop="", obs="", fecha_pago="", banco="", raw_data="{}", unique_id=None):
    conn = get_db_connection()
    c = conn.cursor()
    clean_nit = re.sub(r'\D', '', str(nit_prov))
    t_id = unique_id if unique_id else f"{tenant_nit}_{clean_nit}_{doc_ref}"
    c.execute("""
        INSERT OR REPLACE INTO treasury 
        (id, tenant_nit, doc_ref, proveedor, nit_proveedor, fecha_recibido, fecha_vencimiento, concepto, centro_costo, valor_con_iva, total_pagar, estado, clasificacion, fecha_propuesta, observacion, fecha_pago, banco_girador, raw_data) 
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (t_id, tenant_nit, doc_ref, prov, clean_nit, fecha_recibido, fecha_venc, concepto, cc, val_iva, tot_pagar, estado, clasificacion, fecha_prop, obs, fecha_pago, banco, raw_data))
    conn.commit(); conn.close()

def db_get_treasury(tenant_nit):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, doc_ref, proveedor, nit_proveedor, fecha_recibido, fecha_vencimiento, concepto, centro_costo, valor_con_iva, total_pagar, estado, clasificacion, fecha_propuesta, observacion, fecha_pago, banco_girador, raw_data FROM treasury WHERE tenant_nit=?", (tenant_nit,))
    rows = c.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0], "doc_ref": r[1], "proveedor": r[2], "nit_proveedor": r[3], "fecha_recibido": r[4], "fecha_vencimiento": r[5], "concepto": r[6], "centro_costo": r[7], "valor_con_iva": r[8], "total_pagar": r[9], "estado": r[10], "clasificacion": r[11], "fecha_propuesta": r[12], "observacion": r[13], "fecha_pago": r[14], "banco_girador": r[15], "raw_data": r[16]
        })
    return res

_TABLAS_RESPALDO = {  # tabla: (llave primaria, columnas)
    "tenants": ("nit", ["nit", "razon_social", "siigo_user", "siigo_key", "puc"]),
    "users": ("email", ["email", "password", "nombre", "tenant_nit", "rol", "activo"]),
    "docs": ("id", ["id", "tenant_nit", "doc_ref", "tipo", "estado", "data"]),
    "history": ("id", ["id", "tenant_nit", "doc_ref", "tipo", "fecha", "total", "moneda", "siigo_id", "proveedor", "nit_proveedor", "pdf_b64", "data_json", "usuario"]),
    "treasury": ("id", ["id", "tenant_nit", "doc_ref", "proveedor", "nit_proveedor", "fecha_recibido", "fecha_vencimiento", "concepto", "centro_costo", "valor_con_iva", "total_pagar", "estado", "clasificacion", "fecha_propuesta", "observacion", "fecha_pago", "banco_girador", "raw_data"]),
}

def exportar_respaldo_sqlite():
    """Copia TODOS los datos de la base activa (local o PostgreSQL) a un archivo SQLite descargable. Devuelve los bytes."""
    import tempfile
    ruta = os.path.join(tempfile.gettempdir(), f"respaldo_{os.getpid()}_{int(time.time() * 1000)}.db")
    out = sqlite3.connect(ruta)
    try:
        for ddl in _DDL_TABLAS: out.execute(ddl)
        conn = get_db_connection(); cur = conn.cursor()
        try:
            for tabla, (pk, cols) in _TABLAS_RESPALDO.items():
                cur.execute(f"SELECT {', '.join(cols)} FROM {tabla}")
                out.executemany(f"INSERT OR REPLACE INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", cur.fetchall())
        finally: conn.close()
        out.commit()
    finally: out.close()
    try:
        with open(ruta, "rb") as f: return f.read()
    finally:
        try: os.remove(ruta)
        except Exception: pass

def importar_respaldo_sqlite(datos):
    """Copia (sin borrar nada; si un registro ya existe se actualiza) los datos de un archivo SQLite de AutoCount a la base activa."""
    import tempfile
    ruta = os.path.join(tempfile.gettempdir(), f"import_{os.getpid()}_{int(time.time() * 1000)}.db")
    with open(ruta, "wb") as f: f.write(datos)
    origen = None
    try:
        try:
            origen = sqlite3.connect(ruta)
            ok_integridad = origen.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tablas = {r[0] for r in origen.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError: raise ValueError("El archivo no es una base de datos válida.")
        if not ok_integridad: raise ValueError("El archivo está dañado.")
        if not {"users", "tenants"} <= tablas: raise ValueError("No parece una base de AutoCount (faltan las tablas de usuarios y empresas).")
        conn = get_db_connection(); cur = conn.cursor(); resumen = {}
        try:
            for tabla, (pk, cols) in _TABLAS_RESPALDO.items():
                if tabla not in tablas: resumen[tabla] = 0; continue
                existentes = {r[1] for r in origen.execute(f"PRAGMA table_info({tabla})")}
                usar = [c for c in cols if c in existentes]
                filas = origen.execute(f"SELECT {', '.join(usar)} FROM {tabla}").fetchall()
                sql = f"INSERT OR REPLACE INTO {tabla} ({', '.join(usar)}) VALUES ({', '.join('?' * len(usar))})"
                for fila in filas: cur.execute(sql, fila)
                resumen[tabla] = len(filas)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: conn.close()
        return resumen
    finally:
        if origen is not None: origen.close()
        try: os.remove(ruta)
        except Exception: pass

# ==========================================
# 4. SIIGO API & INTEGRACIONES
# ==========================================
@st.cache_data(ttl=3600)
def obtener_token_siigo(tenant_nit, user, key):
    try:
        res = requests.post("https://api.siigo.com/auth", json={"username": user, "access_key": key}, headers={"Content-Type": "application/json"}, timeout=10)
        return (res.json().get("access_token"), None) if res.status_code == 200 else (None, f"HTTP {res.status_code}: {res.text}")
    except Exception as e: return None, str(e)

def get_siigo_headers(tenant_nit, siigo_user, siigo_key):
    token, err = obtener_token_siigo(tenant_nit, siigo_user, siigo_key)
    return ({"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Partner-Id": "SandboxSiigo"}, None) if token else (None, err)

@st.cache_data(ttl=1800)
def cargar_maestros_siigo(tenant_nit, siigo_user, siigo_key):
    headers, err = get_siigo_headers(tenant_nit, siigo_user, siigo_key)
    maestros = {"doc_types_fc": [], "doc_types_ds": [], "doc_types_cc": [], "impuestos_iva": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_rete": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_ica": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "impuestos_reteiva": [{"id": 0, "nombre": "Ninguno (0%)", "porcentaje": 0}], "pagos": [], "centros_costo": [], "terceros": {}, "terceros_lista": [], "productos": [], "error": err}
    if not headers: return maestros

    try:
        for dt in requests.get("https://api.siigo.com/v1/document-types?type=FC", headers=headers).json(): maestros["doc_types_fc"].append({"id": dt["id"], "nombre": f"FC - {dt['code']} - {dt['name']}"})
    except Exception: pass
    
    try:
        for dt in requests.get("https://api.siigo.com/v1/document-types?type=DS", headers=headers).json(): maestros["doc_types_ds"].append({"id": dt["id"], "nombre": f"DS - {dt['code']} - {dt['name']}"})
    except Exception: pass
    
    try:
        res_cc = requests.get("https://api.siigo.com/v1/document-types?type=Journal", headers=headers)
        if res_cc.status_code == 200 and isinstance(res_cc.json(), list):
            for dt in res_cc.json(): maestros["doc_types_cc"].append({"id": dt["id"], "nombre": f"CC - {dt['code']} - {dt['name']}"})
    except Exception: pass
    
    try:
        for i in requests.get("https://api.siigo.com/v1/taxes", headers=headers).json():
            if i.get("active", True):
                item = {"id": i["id"], "nombre": f"{i['name']} {i['percentage']}%", "porcentaje": float(i.get("percentage", 0))}
                tipo, nombre = (i.get("type") or "").upper(), (i.get("name") or "").upper()
                if "RETEIVA" in tipo or "RETEIVA" in nombre: maestros["impuestos_reteiva"].append(item)
                elif "IVA" in tipo and "RETE" not in tipo: maestros["impuestos_iva"].append(item)
                elif "ICA" in tipo: maestros["impuestos_ica"].append(item)
                else: maestros["impuestos_rete"].append(item)
    except Exception: pass

    try:
        for p in requests.get("https://api.siigo.com/v1/payment-types?document_type=FC", headers=headers).json(): maestros["pagos"].append({"id": p["id"], "nombre": p['name']})
    except Exception: pass

    try:
        for cc in requests.get("https://api.siigo.com/v1/cost-centers", headers=headers).json(): maestros["centros_costo"].append({"id": cc["id"], "nombre": f"{cc['code']} - {cc['name']}"})
    except Exception: pass
    
    try:
        page = 1
        while True:
            res_terc = requests.get(f"https://api.siigo.com/v1/customers?page={page}&page_size=100", headers=headers)
            if res_terc.status_code != 200 or not res_terc.json().get("results"): break
            for t in res_terc.json().get("results", []):
                nit = str(t.get("identification", "")).strip()
                nombre = t.get("name", [""])[0] if isinstance(t.get("name"), list) else t.get("person_name", {}).get("first_name", "Proveedor")
                if nit: maestros["terceros"][nit] = f"{nit} - {nombre}"; maestros["terceros_lista"].append(f"{nit} - {nombre}")
            page += 1
    except Exception: pass

    try:
        page = 1
        while True:
            res_prod = requests.get(f"https://api.siigo.com/v1/products?page={page}&page_size=100", headers=headers)
            if res_prod.status_code != 200 or not res_prod.json().get("results"): break
            for pr in res_prod.json().get("results", []): maestros["productos"].append(f"{pr.get('code')} - {pr.get('name')}")
            page += 1
    except Exception: pass

    return maestros

def crear_comprobante_ajuste_siigo(payload, tenant_nit, siigo_user, siigo_key):
    headers, err = get_siigo_headers(tenant_nit, siigo_user, siigo_key)
    if not headers: return False, f"No Token: {err}"
    url_envio = "https://api.siigo.com/v1/journals"
    try:
        res = requests.post(url_envio, json=payload, headers=headers, timeout=10)
        if res.status_code in [200, 201]:
            res_json = res.json()
            return True, f"✅ Comprobante de Ajuste creado en Siigo (No. {res_json.get('name', 'Generado')})"
        else:
            return False, f"❌ ERROR de Siigo: {res.text}"
    except Exception as e:
        return False, f"❌ Error de Red conectando con Siigo: {e}"

def crear_tercero_express_siigo(nit, nombre, tenant_nit, siigo_user, siigo_key, apellidos="", es_empresa=True, id_type="31", email="", telefono="", direccion="", ciudad_dane="11001", resp_fiscal="R-99-PN", tipo_tercero_str="Supplier"):
    headers, err = get_siigo_headers(tenant_nit, siigo_user, siigo_key)
    if not headers: return False, f"Error Auth: {err}"
    nit_limpio = re.sub(r'\D', '', str(nit))
    if not nit_limpio: return False, "❌ Error: NIT vacío o inválido"

    tipo_terc = "Customer" if "Cliente" in tipo_tercero_str else ("Other" if "Otro" in tipo_tercero_str else "Supplier")
    ciudad_clean = re.sub(r'\D', '', str(ciudad_dane)) or "11001"
    
    payload = {
        "type": tipo_terc, "person_type": "Company" if es_empresa else "Person", "id_type": str(id_type), "identification": nit_limpio,
        "name": [nombre] if es_empresa else [nombre, apellidos or "N/A"],
        "address": {"address": direccion or "Carrera 1 # 1-1", "city": {"country_code": "Co", "state_code": ciudad_clean[:2] if len(ciudad_clean)>=2 else "11", "city_code": ciudad_clean if len(ciudad_clean)==5 else "11001"}},
        "phones": [{"indicative": "57", "number": telefono or "3000000000"}],
        "contacts": [{"first_name": nombre[:50], "last_name": (apellidos or "Contacto")[:50], "email": email or "contacto@proveedor.com"}],
        "fiscal_responsibilities": [{"code": resp_fiscal or "R-99-PN"}]
    }

    try:
        res = requests.post("https://api.siigo.com/v1/customers", json=payload, headers=headers, timeout=10)
        if res.status_code in [200, 201]: return True, f"✅ Tercero Creado con Éxito en Siigo (NIT: {nit_limpio})"
        else: return False, f"❌ Error Siigo API ({res.status_code}): {res.text}"
    except Exception as e: return False, f"❌ Error de Conexión: {e}"

def causar_en_siigo_api(payload, is_ds, tenant_nit, siigo_user, siigo_key):
    headers, err = get_siigo_headers(tenant_nit, siigo_user, siigo_key)
    if not headers: return False, f"No Token: {err}", None, None, None
    url_envio = "https://api.siigo.com/v1/purchase-support-documents" if is_ds else "https://api.siigo.com/v1/purchases"
    real_total = payload["payments"][0]["value"]
    try:
        res = requests.post(url_envio, json=payload, headers=headers, timeout=10)
        res_json = res.json()
        if res.status_code == 400 and res_json.get("errors") and res_json["errors"][0].get("code") == "invalid_total_payments":
            match = re.search(r'calculated is (\d+(\.\d+)?)', res_json["errors"][0].get("message", ""))
            if match:
                extracted_val = float(match.group(1))
                if extracted_val < (real_total * 2):
                    real_total = round(extracted_val, 2)
                    payload["payments"][0]["value"] = real_total
                    res = requests.post(url_envio, json=payload, headers=headers, timeout=10)
                    res_json = res.json()

        if res.status_code in [200, 201]: return True, f"✅ EXITOSO en Siigo", res_json.get("id"), res_json.get('name') or f"{'DS' if is_ds else 'Compra'} No. {res_json.get('number', '')}", real_total
        else: return False, f"❌ ERROR: {res.text}", None, None, None
    except Exception as e: return False, f"❌ Error de Red: {e}", None, None, None

# ==========================================
# 5. PROCESAMIENTO Y PARSEO DE ARCHIVOS
# ==========================================
def _o(a, b):
    """Equivale a `a or b` para elementos XML (falsy = sin hijos), sin usar la comprobación de verdad obsoleta de ElementTree."""
    return a if (a is not None and len(a) > 0) else b

def parse_ubl_xml(xml_content, pdf_bytes_adjunto=None, tenant_nit=None):
    try:
        root = ET.fromstring(xml_content)
        for elem in root.iter():
            if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
        if root.tag in ["ApplicationResponse", "Event"]: return None
        if root.tag == "AttachedDocument":
            attachment = root.find(".//Attachment/ExternalReference/Description")
            if attachment is not None and attachment.text:
                try:
                    root = ET.fromstring(attachment.text)
                    for elem in root.iter():
                        if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
                except Exception: pass
        if root.tag in ["ApplicationResponse", "Event"]: return None
        if tenant_nit:
            customer_node = root.find(".//AccountingCustomerParty")
            if customer_node is not None:
                cust_nit = customer_node.findtext(".//CompanyID") or customer_node.findtext(".//PartyIdentification/ID") or ""
                if re.sub(r'\D', '', str(cust_nit)) and re.sub(r'\D', '', str(tenant_nit)) and re.sub(r'\D', '', str(cust_nit)) != re.sub(r'\D', '', str(tenant_nit)): return None

        factura_id_raw = root.findtext(".//ID") or "1"
        factura_id_solo_num = re.sub(r'\D', '', factura_id_raw) or factura_id_raw
        fecha = root.findtext(".//IssueDate") or datetime.now().strftime("%Y-%m-%d")
        fecha_venc = root.findtext(".//DueDate") or root.findtext(".//PaymentDueDate") or fecha
        
        supplier_node = root.find(".//AccountingSupplierParty")
        supplier_name, supplier_nit = "", ""
        if supplier_node is not None:
            supplier_name = supplier_node.findtext(".//RegistrationName") or supplier_node.findtext(".//PartyName/Name") or supplier_node.findtext(".//Name") or supplier_node.findtext(".//PartyLegalEntity/RegistrationName") or ""
            supplier_nit = supplier_node.findtext(".//CompanyID") or supplier_node.findtext(".//PartyIdentification/ID") or ""
        if not supplier_name: supplier_name = root.findtext(".//RegistrationName") or root.findtext(".//PartyName/Name") or "Proveedor Sin Nombre"
        if not supplier_nit: supplier_nit = root.findtext(".//CompanyID") or root.findtext(".//PartyIdentification/ID") or ""

        pdf_bytes = pdf_bytes_adjunto
        if not pdf_bytes:
            for b64_node in root.findall(".//EmbeddedDocumentBinaryObject"):
                if b64_node.text:
                    try: pdf_bytes = safe_b64decode(b64_node.text); break
                    except Exception: pass

        lineas_detalle, subtotal_factura, iva_factura = [], 0.0, 0.0
        for linea in root.findall(".//InvoiceLine") or root.findall(".//CreditNoteLine"):
            desc_node = _o(linea.find(".//Item/Description"), linea.find(".//Description"))
            concepto = desc_node.text if desc_node is not None else "Sin descripción"
            qty = float(linea.findtext(".//InvoicedQuantity") or linea.findtext(".//CreditedQuantity") or 1.0)
            precio_uni = float(linea.findtext(".//Price/PriceAmount") or 0.0)
            subtotal_linea = float(linea.findtext(".//LineExtensionAmount") or (qty * precio_uni))

            iva_pct, iva_valor = 0.0, 0.0
            for tax in linea.findall(".//TaxTotal/TaxSubtotal"):
                tax_name = tax.findtext(".//TaxScheme/Name") or ""
                if tax.findtext(".//TaxScheme/ID") == "01" or "IVA" in tax_name.upper():
                    iva_pct, iva_valor = float(tax.findtext(".//Percent") or 0), float(tax.findtext(".//TaxAmount") or 0)

            lineas_detalle.append({"Concepto": concepto, "Cantidad": qty, "Valor Unitario": precio_uni, "Subtotal": subtotal_linea, "IVA %": iva_pct, "Valor IVA": iva_valor, "Total Línea": subtotal_linea + iva_valor})
            subtotal_factura += subtotal_linea; iva_factura += iva_valor

        monetary_node = _o(root.find(".//LegalMonetaryTotal"), root.find(".//RequestedMonetaryTotal"))
        total_oficial = float(monetary_node.findtext(".//PayableAmount") or (subtotal_factura + iva_factura)) if monetary_node is not None else (subtotal_factura + iva_factura)
        tipo_doc = "Nota Crédito" if root.tag == "CreditNote" else "Factura"

        return {"tipo_origen": "FC", "Resumen": {"Tipo": tipo_doc, "ID": factura_id_solo_num, "Fecha": fecha, "FechaVencimiento": fecha_venc, "NIT": supplier_nit, "Proveedor": supplier_name, "Subtotal": subtotal_factura, "IVA": iva_factura, "Retenciones": 0.0, "TotalPagar": total_oficial, "Total": total_oficial, "Estado": "Pendiente", "Moneda": "COP", "CentroCosto": None, "Clasificacion_Teso": "Proveedor"}, "Detalle": lineas_detalle, "pdf_b64": base64.b64encode(pdf_bytes).decode('utf-8') if pdf_bytes else None}
    except Exception: return None

def process_bytes(file_name, file_bytes, data_list, tenant_nit=None):
    if file_name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                pdf_map = {os.path.splitext(f)[0]: z.read(f) for f in z.namelist() if f.lower().endswith(".pdf")}
                for f in z.namelist():
                    if f.lower().endswith(".xml"):
                        xml_bytes = z.read(f)
                        parsed = parse_ubl_xml(xml_bytes, pdf_map.get(os.path.splitext(f)[0]) or (list(pdf_map.values())[0] if pdf_map else None), tenant_nit)
                        if parsed: parsed["xml_b64"] = base64.b64encode(xml_bytes).decode("utf-8"); data_list.append(parsed)
        except Exception: pass
    elif file_name.lower().endswith(".xml"):
        parsed = parse_ubl_xml(file_bytes, tenant_nit=tenant_nit)
        if parsed: parsed["xml_b64"] = base64.b64encode(file_bytes).decode("utf-8"); data_list.append(parsed)

def extraer_facturas_desde_drive_cloud(web_app_url, data_list, tenant_nit=None, stats=None):
    """Lee los archivos del script de Drive. Igual que siempre, pero si se pasa `stats` deja un resumen de lo recibido."""
    try:
        res = requests.get(web_app_url, timeout=300)
        if res.status_code == 200:
            try: archivos = res.json()
            except Exception:
                return False, f"Drive respondió algo que NO es JSON (¿cambió el permiso o la URL del script?). Inicio de la respuesta: {res.text[:120]!r}"
            if stats is not None:
                stats["archivos"] = len(archivos) if isinstance(archivos, list) else 0
                exts = {}
                for it in (archivos if isinstance(archivos, list) else []):
                    ext = os.path.splitext(str(it.get("filename", "")))[1].lower().strip(".") or "sin extensión"
                    exts[ext] = exts.get(ext, 0) + 1
                stats["detalle"] = ", ".join(f"{n} {e}" for e, n in exts.items())
            if not isinstance(archivos, list) or len(archivos) == 0: return True, "No se encontraron archivos."
            for item in archivos:
                fname = item.get("filename", "factura.zip")
                b64_str = item.get("base64", "")
                if b64_str: process_bytes(fname, safe_b64decode(b64_str), data_list, tenant_nit=tenant_nit)
            return True, f"Se leyeron {len(archivos)} archivo(s) desde Drive."
        else: return False, f"Error HTTP {res.status_code} al conectar con Drive."
    except Exception as e: return False, f"Error Drive: {e}"

def extraer_datos_pdf_soporte(pdf_bytes, filename):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        texto_crudo = "".join([reader.pages[i].extract_text() or "" for i in range(min(len(reader.pages), 2))])
    except Exception: texto_crudo = ""

    _api_key = _secret("OPENAI_API_KEY")
    prompt = f"""Eres un auditor contable en Colombia. Procesando para "Davinci Technologies SAS" (NIT 900557218).
    REGLA 1: NUNCA extraigas "Davinci Technologies" ni "900557218". Ellos son el CLIENTE.
    REGLA 2: Encuentra a la PERSONA o EMPRESA QUE COBRA. Ignora planillas de EPS/Aportes.
    Devuelve JSON válido:
    {{"proveedor": "Nombre exacto", "nit": "NIT solo números", "fecha": "YYYY-MM-DD", "total": Total a pagar decimal, "moneda": "COP o USD", "documento_ref": "Número factura"}}
    Texto: {texto_crudo}"""
    try:
        client = OpenAI(api_key=_api_key)
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "system", "content": "Solo JSON."}, {"role": "user", "content": prompt}], response_format={"type": "json_object"}, temperature=0.0)
        datos_ia = json.loads(response.choices[0].message.content)
    except Exception: datos_ia = {"proveedor": "Error IA", "nit": "900123456", "documento_ref": "101", "fecha": datetime.now().strftime("%Y-%m-%d"), "moneda": "COP", "total": 0.0}

    moneda_final, fecha_final = datos_ia.get("moneda", "COP"), datos_ia.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    return {"tipo_origen": "DS", "archivo": filename, "pdf_b64": base64.b64encode(pdf_bytes).decode('utf-8') if pdf_bytes else None, "proveedor": datos_ia.get("proveedor", "Desconocido"), "nit": re.sub(r'\D', '', str(datos_ia.get("nit", "900123456"))), "documento_ref": re.sub(r'\D', '', str(datos_ia.get("documento_ref", "101"))), "fecha": fecha_final, "moneda_origen": moneda_final, "monto_origen": float(datos_ia.get("total", 0.0)), "trm": consultar_trm_oficial_script(fecha_final) if moneda_final == "USD" else 1.0, "estado": "Pendiente", "centro_costo": None, "causado": False, "Clasificacion_Teso": "Proveedor", "items_custom": []}

def procesar_excel_puc(file_obj):
    try:
        df = pd.read_excel(file_obj) if file_obj.name.endswith(('.xlsx', '.xls')) else pd.read_csv(file_obj)
        cols = list(df.columns)
        code_col, name_col, level_col, status_col = cols[0] if cols else None, cols[1] if len(cols)>1 else None, None, None
        for c in cols:
            c_str = str(c).lower().strip()
            if any(k in c_str for k in ["cód", "cod", "cuenta", "numero"]): code_col = c
            if any(k in c_str for k in ["nom", "desc", "concepto", "denominaci"]): name_col = c
            if any(k in c_str for k in ["nivel", "agrupaci", "tipo_cuenta", "clase"]): level_col = c
            if any(k in c_str for k in ["estad", "activ", "state"]): status_col = c

        puc_list = []
        if code_col and name_col:
            for _, row in df.iterrows():
                val_code, val_name = str(row[code_col]).strip(), str(row[name_col]).strip()
                code_digits = re.sub(r'[^\d]', '', val_code)
                es_transaccional, es_activa = True, True
                if level_col and pd.notnull(row[level_col]):
                    lev_str = str(row[level_col]).lower().strip()
                    if any(k in lev_str for k in ["agrup", "mayor", "titulo", "subcuenta", "no", "falso", "false", "0"]) and "transaccional" not in lev_str and "auxiliar" not in lev_str: es_transaccional = False
                if status_col and pd.notnull(row[status_col]) and str(row[status_col]).lower().strip() in ["inactiva", "inactivo", "bloqueada", "no", "falso", "false", "0"]: es_activa = False
                if code_digits and val_name.lower() != "nan" and es_transaccional and es_activa: puc_list.append(f"{code_digits} - {val_name}")
        return list(dict.fromkeys(puc_list)) if puc_list else None
    except Exception: return None

def generar_comprobante_pdf(hist_record, tenant_razon_social, tenant_nit):
    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    
    def safe_str(text): return str(text).encode('latin-1', 'replace').decode('latin-1')

    data = json.loads(hist_record.get('data_json', '{}'))
    tipo, moneda = hist_record.get('tipo', 'FC'), hist_record.get('moneda', 'COP')
    
    c_costo_global = "N/A"
    if tipo == 'FC' and "Resumen" in data: c_costo_global = data["Resumen"].get("CentroCosto") or "N/A"
    elif tipo == 'DS': c_costo_global = data.get("centro_costo") or "N/A"
        
    trm = float(data.get("trm", 1.0)) if tipo == 'DS' else 1.0

    pdf.set_font("Arial", 'B', 20); pdf.cell(40, 15, "X", border=0, align="C")
    pdf.set_xy(50, 10); pdf.set_font("Arial", 'B', 10); pdf.cell(100, 5, safe_str(tenant_razon_social), ln=True, align="C")
    pdf.set_x(50); pdf.set_font("Arial", '', 9); pdf.cell(100, 5, safe_str(f"Nit {tenant_nit}"), ln=True, align="C")
    pdf.set_x(50); pdf.cell(100, 5, "Colombia", ln=True, align="C")
    
    pdf.rect(215, 10, 65, 15); pdf.set_xy(215, 12); pdf.set_font("Arial", 'B', 12)
    pdf.cell(65, 5, safe_str("Causacion Contable (FC)" if tipo == 'FC' else "Documento Soporte (DS)"), ln=True, align="C")
    pdf.set_x(215); pdf.set_font("Arial", 'B', 10)
    pdf.cell(65, 5, safe_str(f"No. {hist_record['id_siigo_num'].split('|||')[0] if hist_record.get('id_siigo_num') else 'Pendiente'}"), ln=True, align="C")

    pdf.ln(15); y_info = pdf.get_y()
    pdf.rect(10, y_info, 150, 20); pdf.set_xy(12, y_info + 2); pdf.set_font("Arial", 'B', 9); pdf.cell(20, 5, "Proveedor:")
    pdf.set_font("Arial", '', 9); pdf.cell(120, 5, safe_str(hist_record.get('proveedor', '')))
    pdf.set_xy(12, y_info + 8); pdf.set_font("Arial", 'B', 9); pdf.cell(20, 5, "NIT:")
    pdf.set_font("Arial", '', 9); pdf.cell(45, 5, safe_str(hist_record.get('nit', '')))
    pdf.set_font("Arial", 'B', 9); pdf.cell(20, 5, "C. Costo:")
    pdf.set_font("Arial", '', 9); pdf.cell(40, 5, safe_str(c_costo_global.split(" - ")[0].strip() if " - " in c_costo_global else c_costo_global))
    pdf.set_xy(12, y_info + 14); pdf.set_font("Arial", 'B', 9); pdf.cell(20, 5, "Doc. Prov:")
    pdf.set_font("Arial", '', 9); pdf.cell(120, 5, safe_str(hist_record.get('id_doc_prov', '')))

    pdf.rect(165, y_info, 115, 20); pdf.set_xy(167, y_info + 2); pdf.set_font("Arial", 'B', 9); pdf.cell(35, 5, "Fecha Causacion:")
    pdf.set_font("Arial", '', 9); pdf.cell(30, 5, safe_str(hist_record.get('fecha', '')))
    pdf.set_xy(167, y_info + 8); pdf.set_font("Arial", 'B', 9); pdf.cell(35, 5, "Moneda / TRM:")
    pdf.set_font("Arial", '', 9); pdf.cell(30, 5, safe_str(f"{moneda} / ${trm:,.2f}" if moneda == 'USD' else f"{moneda} / N/A"))
    pdf.set_xy(167, y_info + 14); pdf.set_font("Arial", 'B', 9); pdf.cell(35, 5, "Usuario:")
    pdf.set_font("Arial", '', 9); pdf.cell(30, 5, safe_str(hist_record.get('usuario', '')))

    pdf.ln(10); pdf.set_y(y_info + 25); pdf.set_font("Arial", 'B', 8); pdf.set_fill_color(240, 240, 240)
    col_w, headers = [10, 22, 23, 85, 12, 25, 20, 25, 23, 25], ["Item", "Cta. PUC", "C. Costo", "Descripcion", "Cant", "Vr. Unitario", "Val. Desc", "Impto. Cargo", "Impto. Rete", "Vr. Total"]
    for i in range(len(headers)): pdf.cell(col_w[i], 8, headers[i], border=1, align="C", fill=True)
    pdf.ln(8); pdf.set_font("Arial", '', 8)

    for idx, item in enumerate(data.get("Detalle", []) if tipo == 'FC' else data.get("items_custom", [])):
        if tipo == 'FC': desc, cant, v_unit, iva_pct, iva_val, vr_total, puc = item.get("Concepto", "N/A"), float(item.get("Cantidad", 1)), float(item.get("Valor Unitario", 0)), float(item.get("IVA %", 0)), float(item.get("Valor IVA", 0)), float(item.get("Total Línea", 0)), item.get("Cta_PUC", "N/A")
        else: desc, cant, v_unit, iva_pct = item.get("description", "N/A"), float(item.get("quantity", 1)), float(item.get("price", 0)), float(item.get("pct_iva", 0)); iva_val = (cant * v_unit) * (iva_pct / 100.0); vr_total = (cant * v_unit) + iva_val; puc = item.get("code", "N/A")
        ret_name = str(item.get("Retencion_Nombre", "0%")); rete_str = re.search(r'(\d+(\.\d+)?)%', ret_name).group(0) if re.search(r'(\d+(\.\d+)?)%', ret_name) and ret_name != "0%" else ("0%" if ret_name == "0%" else ret_name[:10])
        pdf.cell(col_w[0], 6, str(idx+1), border=1, align="C"); pdf.cell(col_w[1], 6, safe_str(puc), border=1, align="C"); pdf.cell(col_w[2], 6, safe_str(c_costo_global)[:10], border=1, align="C"); pdf.cell(col_w[3], 6, safe_str(desc)[:55], border=1, align="L"); pdf.cell(col_w[4], 6, f"{cant:.2f}", border=1, align="C"); pdf.cell(col_w[5], 6, f"${v_unit:,.2f}", border=1, align="R"); pdf.cell(col_w[6], 6, "$0.00", border=1, align="R"); pdf.cell(col_w[7], 6, f"{iva_pct}% (${iva_val:,.2f})", border=1, align="R"); pdf.cell(col_w[8], 6, rete_str, border=1, align="C"); pdf.cell(col_w[9], 6, f"${vr_total:,.2f}", border=1, align="R"); pdf.ln(6)

    pdf.ln(5); y_totals = pdf.get_y()
    forma_pago, ret_desglose, subtotal, iva, retenciones, total_pagar = (data.get("Resumen", {}).get("FormaPago", "N/A"), data.get("Resumen", {}).get("Retenciones_Desglose", {}), float(data.get("Resumen", {}).get("Subtotal", 0)), float(data.get("Resumen", {}).get("IVA", 0)), float(data.get("Resumen", {}).get("Retenciones", 0)), float(data.get("Resumen", {}).get("TotalPagar", 0))) if tipo == 'FC' else (data.get("FormaPago", "N/A"), data.get("Retenciones_Desglose", {}), float(data.get("subtotal", 0)), float(data.get("iva", 0)), float(data.get("retenciones", 0)), float(data.get("TotalPagar", 0)))
    
    pdf.set_font("Arial", 'B', 9); pdf.cell(100, 5, "Condiciones de Pago / Observaciones:"); pdf.ln(5); pdf.set_font("Arial", '', 9)
    pdf.multi_cell(140, 5, safe_str(f"Forma de Pago: {forma_pago}\nAuditoria: Valores cruzados exitosamente.\nDocumento generado automaticamente por AutoCount.ai"))
    
    y_curr, mon_lbl = y_totals, f" ({moneda})" if moneda == 'USD' else ""
    pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 9); pdf.cell(50, 6, f"Total Bruto{mon_lbl}", border=1); pdf.set_font("Arial", '', 9); pdf.cell(65, 6, f"${subtotal:,.2f}", border=1, align="R", ln=True); y_curr += 6
    pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 9); pdf.cell(50, 6, f"Impto. Cargo (IVA){mon_lbl}", border=1); pdf.set_font("Arial", '', 9); pdf.cell(65, 6, f"${iva:,.2f}", border=1, align="R", ln=True); y_curr += 6

    if ret_desglose:
        for k, v in ret_desglose.items():
            if float(v) > 0:
                pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 9); pdf.cell(50, 6, safe_str((str(k)[:22] + '..') if len(str(k)) > 24 else str(k)), border=1); pdf.set_font("Arial", '', 9); pdf.cell(65, 6, f"${float(v):,.2f}", border=1, align="R", ln=True); y_curr += 6
    else: pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 9); pdf.cell(50, 6, f"Retenciones{mon_lbl}", border=1); pdf.set_font("Arial", '', 9); pdf.cell(65, 6, f"${retenciones:,.2f}", border=1, align="R", ln=True); y_curr += 6

    pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 10); pdf.set_fill_color(230, 230, 230); pdf.cell(50, 8, f"Total a Pagar{mon_lbl}", border=1, fill=True); pdf.cell(65, 8, f"${total_pagar:,.2f}", border=1, align="R", fill=True, ln=True); y_curr += 8
    if moneda == 'USD' and trm > 1: pdf.set_xy(165, y_curr); pdf.set_font("Arial", 'B', 9); pdf.cell(50, 6, "Equivalente (COP)", border=1); pdf.set_font("Arial", '', 9); pdf.cell(65, 6, f"${total_pagar * trm:,.2f} COP", border=1, align="R", ln=True)

    try: return pdf.output(dest='S').encode('latin-1')
    except Exception: return bytes(pdf.output())

def extraer_valores_reporte(d, h_total=None):
    is_fc = d.get("tipo_origen") == "FC" or "Resumen" in d
    if is_fc:
        r = d.get("Resumen", {})
        detalles = d.get("Detalle", [])
        conceptos_list = [str(i.get("Concepto", "") or i.get("description", "")) for i in detalles]
        conceptos = " | ".join([c for c in conceptos_list if c]) or "Factura de Compra"
        
        if detalles:
            subt = sum(float(i.get("Subtotal", float(i.get("Cantidad", 1)) * float(i.get("Valor Unitario", 0)))) for i in detalles)
            iva = sum(float(i.get("Valor IVA", float(i.get("Subtotal", 0)) * (float(i.get("IVA %", 0))/100.0))) for i in detalles)
        else: subt, iva = float(r.get("Subtotal", h_total or 0)), float(r.get("IVA", 0))
        
        valor_con_iva = round(subt + iva, 2)
        retenciones = float(r.get("Retenciones", 0.0))
        
        if "TotalPagar" in r and float(r["TotalPagar"]) <= (valor_con_iva * 1.5): 
            valor_pagar = float(r["TotalPagar"])
            retenciones = round(valor_con_iva - valor_pagar, 2)
        else: 
            valor_pagar = round(valor_con_iva - retenciones, 2)
            
        clasif = r.get("Clasificacion_Teso", "Proveedor")
            
        return {"fecha_recibido": r.get("Fecha", d.get("fecha", "")), "fecha_vencimiento": r.get("FechaVencimiento", r.get("Fecha", d.get("fecha", ""))), "nit": r.get("NIT", d.get("nit", "")), "proveedor": r.get("Proveedor", d.get("proveedor", "")), "doc_ref": r.get("ID", d.get("doc_ref", "")), "conceptos": conceptos, "centro_costo": r.get("CentroCosto", "-- Sin Centro de Costo --") or "-- Sin Centro de Costo --", "valor_con_iva": valor_con_iva, "retenciones": retenciones, "valor_a_pagar": valor_pagar, "clasificacion": clasif}
    else:
        items = d.get("items_custom", [])
        conceptos_list = [str(i.get("description", "")) for i in items]
        conceptos = " | ".join([c for c in conceptos_list if c]) or "Documento Soporte"
        
        if items:
            subt = sum(float(i.get("price", 0)) * float(i.get("quantity", 1)) for i in items)
            iva = sum(float(i.get("price", 0)) * float(i.get("quantity", 1)) * (float(i.get("pct_iva", 0))/100.0) for i in items)
        else: subt, iva = float(d.get("subtotal", d.get("monto_origen", h_total or 0))), float(d.get("iva", 0))
        
        valor_con_iva = round(subt + iva, 2)
        retenciones = float(d.get("retenciones", 0.0))
        
        if "TotalPagar" in d and float(d["TotalPagar"]) <= (valor_con_iva * 1.5): 
            valor_pagar = float(d["TotalPagar"])
            retenciones = round(valor_con_iva - valor_pagar, 2)
        else: 
            valor_pagar = round(valor_con_iva - retenciones, 2)
            
        clasif = d.get("Clasificacion_Teso", "Proveedor")
            
        return {"fecha_recibido": d.get("fecha", ""), "fecha_vencimiento": d.get("FechaVencimiento", d.get("fecha", "")), "nit": d.get("nit", ""), "proveedor": d.get("proveedor", ""), "doc_ref": d.get("documento_ref", d.get("doc_ref", "")), "conceptos": conceptos, "centro_costo": d.get("centro_costo", "-- Sin Centro de Costo --") or "-- Sin Centro de Costo --", "valor_con_iva": valor_con_iva, "retenciones": retenciones, "valor_a_pagar": valor_pagar, "clasificacion": clasif}

# ==========================================
# 6. MODALES DE INTERFAZ (UI)
# ==========================================
@st.dialog("📝 Crear / Editar Tercero en Siigo")
def modal_formulario_tercero(nit_def, nombre_def, tenant_nit, siigo_user, siigo_key, es_extranjero=False):
    st.caption("Verifique y ajuste los datos del tercero antes de crearlo en Siigo:")
    with st.form("form_modal_tercero"):
        col1, col2 = st.columns(2)
        with col1:
            tipo_tercero_ui = st.selectbox("Tipo de Tercero", ["Proveedor (Supplier)", "Cliente (Customer)", "Otro (Other)"])
            tipo_persona_ui = st.selectbox("Tipo de Persona", ["Empresa (Company)", "Persona Natural (Person)"], index=0 if any(k in nombre_def.upper() for k in ["S.A.S", "INC", "LLC", "LTD", "SA"]) else 1)
            id_type_ui = st.selectbox("Tipo Identificación", ["31 - NIT", "13 - Cédula de Ciudadanía", "50 - NIT Extranjero", "42 - Documento Identificación Extranjero"], index=2 if es_extranjero else (0 if "Empresa" in tipo_persona_ui else 1))
            nit_ui = st.text_input("NIT / Cédula (Solo números)", value=re.sub(r'\D', '', str(nit_def)))
        with col2:
            nombre_ui = st.text_input("Razón Social / Nombre", value=nombre_def)
            apellidos_ui = st.text_input("Apellidos (Solo Persona Natural)", value="")
            correo_ui = st.text_input("Correo Electrónico", value="contacto@proveedor.com")
            telefono_ui = st.text_input("Teléfono / Celular", value="3112289967")

        st.markdown("---")
        col3, col4 = st.columns(2)
        with col3: direccion_ui = st.text_input("Dirección", value="Calle 86 A - No 13-09")
        with col4:
            ciudad_ui = st.selectbox("Ciudad (DANE)", ["11001 - Bogotá", "05001 - Medellín", "76001 - Cali", "08001 - Barranquilla", "68001 - Bucaramanga"])
            resp_fiscal_ui = st.selectbox("Responsabilidad Fiscal", ["R-99-PN - No aplica - Otros", "O-13 - Gran contribuyente", "O-15 - Autorretenedor", "O-23 - Agente de retención IVA", "O-47 - Régimen simple de tributación", "O-48 - Impuesto sobre las ventas - IVA"])

        if st.form_submit_button("🚀 Guardar y Crear Tercero en Siigo API", type="primary", use_container_width=True):
            exito_c, msg_c = crear_tercero_express_siigo(nit_ui, nombre_ui, tenant_nit, siigo_user, siigo_key, apellidos_ui, "Empresa" in tipo_persona_ui, id_type_ui.split(" - ")[0].strip(), correo_ui, telefono_ui, direccion_ui, ciudad_ui.split(" - ")[0].strip(), resp_fiscal_ui.split(" - ")[0].strip(), tipo_tercero_ui)
            if exito_c: st.success(msg_c); st.rerun()
            else: st.error(msg_c)

@st.dialog("⚖️ Ajuste de ReteICA / AIU (Comprobante Contable)")
def modal_ajuste_ica(hist_record, maestros, curr_tenant_puc, tenant_nit, siigo_user, siigo_key, curr_user_email):
    st.markdown(f"**Ajuste para Factura:** {hist_record['id_doc_prov']} - {hist_record['proveedor']}")
    st.caption("Este comprobante bajará el saldo real de la factura cruzando contra la 22050501 y actualizará los reportes.")
    
    data_inv = json.loads(hist_record.get('data_json', '{}'))
    moneda_fra = hist_record.get('moneda', 'COP')
    tipo = hist_record.get('tipo', 'FC')
    
    if tipo == 'FC' and "Resumen" in data_inv: cc_original = data_inv["Resumen"].get("CentroCosto")
    else: cc_original = data_inv.get("centro_costo")
        
    trm_fra = float(data_inv.get('trm', 1.0)) if tipo == 'DS' else float(data_inv.get('Resumen', {}).get('TRM', 1.0))
    if trm_fra <= 0: trm_fra = 1.0
    
    lista_impuestos = maestros.get("impuestos_ica", []) + maestros.get("impuestos_rete", []) + maestros.get("impuestos_reteiva", [])
    
    with st.form("form_cc_ajuste"):
        val_ajuste = st.number_input(f"Valor a Ajustar (ReteICA a descontar de la CXP en {moneda_fra})", min_value=0.0, value=0.0, step=1000.0, key=f"val_{hist_record['id_doc_prov']}")
        num_cc = st.number_input("Número de Comprobante (Requerido)", value=int(datetime.now().strftime("%d%H%M%S")), step=1, key=f"num_{hist_record['id_doc_prov']}")
        
        cc_lista = maestros.get("centros_costo", [])
        cc_opts = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
        cc_sel = st.selectbox("Centro de Costo (Heredado de la Factura)", options=cc_opts, index=cc_opts.index(cc_original) if cc_original in cc_opts else 0, key=f"cc_{hist_record['id_doc_prov']}")
        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_sel), None) if cc_sel != "-- Sin Centro de Costo --" else None
        
        id_type_cc = 19163
        st.info(f"📌 Usando Comprobante Contable Fijo (ID interno Siigo: {id_type_cc}) | Moneda: {moneda_fra}")
        
        st.text_input("Cuenta por Pagar (Débito - Baja saldo proveedor)", value="22050501", disabled=True)
        cta_ica_sel = st.selectbox("Cuenta ReteICA (Crédito - Cuenta PUC)", options=curr_tenant_puc, key=f"cta_{hist_record['id_doc_prov']}")
        cta_ica_code = re.sub(r'[^\d]', '', cta_ica_sel.split(" - ")[0].strip())
        impuesto_sel = st.selectbox("Impuesto asociado en Siigo (Requerido por API)", options=[i["nombre"] for i in lista_impuestos], key=f"imp_{hist_record['id_doc_prov']}")
        id_impuesto = next((i["id"] for i in lista_impuestos if i["nombre"] == impuesto_sel), 0)
        
        if st.form_submit_button("🚀 Generar Comprobante en Siigo"):
            if val_ajuste <= 0: st.error("El valor a ajustar debe ser mayor a 0.")
            elif not num_cc: st.error("El número de comprobante es requerido.")
            elif id_impuesto == 0: st.error("Seleccione un impuesto válido de la lista.")
            else:
                fecha_hoy = datetime.now().strftime("%Y-%m-%d")
                fecha_fra_original = hist_record['fecha']
                num_prov_clean = re.sub(r'\D', '', str(hist_record['id_doc_prov'])) or "1"
                
                prefijo_siigo, consecutivo_siigo = ("FC", int(str(num_prov_clean)[:9])) if hist_record['tipo'] == 'FC' else ("DS", int(str(num_prov_clean)[-10:]))
                
                item_debito = {"account": {"code": "22050501", "movement": "Debit"}, "customer": {"identification": str(hist_record['nit'])}, "description": f"Ajuste ReteICA FC {hist_record['id_doc_prov']}", "value": float(val_ajuste), "due": {"prefix": prefijo_siigo, "consecutive": consecutivo_siigo, "quote": 1, "date": fecha_fra_original}}
                if id_cc_head: item_debito["cost_center"] = id_cc_head
                
                item_credito = {"account": {"code": cta_ica_code, "movement": "Credit"}, "customer": {"identification": str(hist_record['nit'])}, "description": f"Ajuste ReteICA FC {hist_record['id_doc_prov']}", "value": float(val_ajuste), "tax": {"id": int(id_impuesto)}}
                if id_cc_head: item_credito["cost_center"] = id_cc_head
                
                payload_cc = {"document": {"id": id_type_cc}, "number": int(num_cc), "date": fecha_hoy, "reference": str(hist_record['id_doc_prov']), "observations": f"Ajuste ReteICA/AIU Factura {hist_record['id_doc_prov']}", "items": [item_debito, item_credito]}
                if moneda_fra == 'USD': payload_cc["currency"] = {"code": "USD", "exchange_rate": trm_fra}
                
                exito, msg = crear_comprobante_ajuste_siigo(payload_cc, tenant_nit, siigo_user, siigo_key)
                if exito:
                    if tipo == 'FC' and "Resumen" in data_inv:
                        data_inv["Resumen"]["TotalPagar"] -= float(val_ajuste)
                        if "Retenciones_Desglose" not in data_inv["Resumen"]: data_inv["Resumen"]["Retenciones_Desglose"] = {}
                        data_inv["Resumen"]["Retenciones_Desglose"][impuesto_sel] = val_ajuste
                        data_inv["Resumen"]["Retenciones"] = data_inv["Resumen"].get("Retenciones", 0) + float(val_ajuste)
                    else:
                        data_inv["TotalPagar"] = data_inv.get("TotalPagar", hist_record['total']) - float(val_ajuste)
                        if "Retenciones_Desglose" not in data_inv: data_inv["Retenciones_Desglose"] = {}
                        data_inv["Retenciones_Desglose"][impuesto_sel] = val_ajuste
                        data_inv["retenciones"] = data_inv.get("retenciones", 0) + float(val_ajuste)

                    pdf_str = data_inv.get("pdf_b64") or (base64.b64encode(hist_record['pdf_original']).decode('utf-8') if hist_record.get('pdf_original') else None)
                    db_save_history(tenant_nit, hist_record['id_doc_prov'], hist_record['tipo'], hist_record['fecha'], hist_record['total'] - float(val_ajuste), moneda_fra, hist_record['id_siigo_num'], hist_record['proveedor'], hist_record['nit'], pdf_str, json.dumps(data_inv), curr_user_email)
                    # 🔔 WEBHOOK GOOGLE SHEETS (AJUSTE RETEICA -> baja el Valor a Pagar)
                    try:
                        if tipo == 'FC' and "Resumen" in data_inv:
                            _nuevo_pagar = float(data_inv["Resumen"].get("TotalPagar", 0))
                            _concepto = " | ".join([str(i.get("Concepto", "")) for i in data_inv.get("Detalle", [])]) or "Factura de Compra"
                            _cc = data_inv["Resumen"].get("CentroCosto") or ""
                            _clasif = data_inv["Resumen"].get("Clasificacion_Teso", "Proveedor")
                        else:
                            _nuevo_pagar = float(data_inv.get("TotalPagar", 0))
                            _concepto = " | ".join([str(i.get("description", "")) for i in data_inv.get("items_custom", [])]) or "Documento Soporte"
                            _cc = data_inv.get("centro_costo") or ""
                            _clasif = data_inv.get("Clasificacion_Teso", "Proveedor")
                        _destino_doc = (data_inv.get("Resumen", {}).get("Clasificacion") if (tipo == 'FC' and "Resumen" in data_inv) else data_inv.get("Clasificacion")) or "CXP"
                        _ten = db_get_tenant(tenant_nit) or {"razon_social": "DAVINCI", "nit": tenant_nit}
                        _rec_adj = {"id_doc_prov": hist_record['id_doc_prov'], "tipo": hist_record['tipo'], "fecha": hist_record['fecha'], "total": hist_record['total'] - float(val_ajuste), "moneda": moneda_fra, "id_siigo_num": hist_record['id_siigo_num'], "proveedor": hist_record['proveedor'], "nit": hist_record['nit'], "data_json": json.dumps(data_inv), "usuario": curr_user_email}
                        _adj_ajuste = armar_adjuntos_webhook(_rec_adj, _ten, solo_causacion=True)
                        enviar_fila_webhook(construir_fila_webhook(
                            "Ajuste ReteICA", _ten, curr_user_email, tipo,
                            re.sub(r'\D', '', str(hist_record['id_doc_prov'])) or hist_record['id_doc_prov'], "",
                            hist_record['proveedor'], hist_record['nit'], hist_record['fecha'], hist_record['fecha'],
                            moneda_fra, 1.0, _cc, _concepto, 0.0, 0.0, float(val_ajuste),
                            float(hist_record['total']), _nuevo_pagar, "", _clasif, "",
                            obs=f"Ajuste ReteICA/AIU por {float(val_ajuste):,.0f}"), "Ajuste ReteICA", adjuntos=_adj_ajuste, destino=_destino_doc)
                    except Exception as _e_adj:
                        print(f"[Webhook Sheets] Error armando ajuste: {_e_adj}")
                    st.success(msg); st.balloons(); st.rerun()
                else: st.error(msg)

# ==========================================
# 7. FLUJO DE AUTENTICACIÓN
# ==========================================
if 'authenticated_user' not in st.session_state: st.session_state['authenticated_user'] = None

if st.session_state['authenticated_user'] is None:
    st.markdown(f"""
    <div style='text-align: center; margin-top: 50px;'>
        <img src='{LOGO_URL}' height='65' style='border-radius: 8px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
        <div style='font-size: 2.2rem; font-weight: 900; color: #0f172a; letter-spacing: -1px;'>AutoCount<span style='color: #38bdf8;'>.ai</span></div>
        <p style='color: #64748b; font-size: 0.95rem;'>SaaS de Causación e Integración Contable</p>
        <div><span class='chip-lite'>Siigo</span><span class='chip-lite'>Google Sheets</span><span class='chip-lite'>IA</span></div>
    </div>
    """, unsafe_allow_html=True)
    if st.session_state.get('aviso_login'): st.warning(st.session_state.pop('aviso_login'))
    with st.container():
        st.markdown("<div class='login-box'>", unsafe_allow_html=True)
        with st.form("form_login"):
            st.subheader("Acceso Seguro")
            email_in = st.text_input("Correo Electrónico")
            pass_in = st.text_input("Contraseña", type="password")
            if st.form_submit_button("🚀 Entrar a la Plataforma", type="primary", use_container_width=True):
                _espera = _segundos_bloqueo(email_in)
                user_info = None if _espera > 0 else db_auth_user(email_in, pass_in)
                if _espera > 0: st.error(f"🔒 Demasiados intentos fallidos. Intenta de nuevo en {_espera // 60 + 1} minuto(s).")
                elif user_info:
                    _limpiar_intentos(email_in)
                    st.session_state['authenticated_user'] = user_info
                    st.toast(f"¡Bienvenido, {user_info['nombre']}!", icon="🎉")
                    st.rerun()
                elif db_usuario_desactivado(email_in, pass_in):
                    st.error("🚫 Tu usuario está desactivado. Comunícate con el administrador.")
                else:
                    _registrar_fallo(email_in)
                    st.error("❌ Correo o contraseña incorrectos.")
        st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

# ==========================================
# 8. SESIÓN Y PERMISOS DE USUARIOS
# ==========================================
curr_user = st.session_state['authenticated_user']
_vigente = db_usuario_vigente(curr_user['email'])
if _vigente is None:   # lo desactivaron o eliminaron mientras tenía la sesión abierta
    st.session_state['authenticated_user'] = None
    st.session_state['aviso_login'] = "Tu usuario fue desactivado o eliminado. Comunícate con el administrador."
    st.rerun()
curr_user.update({"nombre": _vigente["nombre"], "rol": _vigente["rol"]})   # un cambio de rol se aplica sin volver a entrar
curr_rol = curr_user['rol']

_ini_user = "".join([p[0] for p in str(curr_user['nombre']).split()[:2]]).upper() or "U"
st.sidebar.markdown(
    f"<div class='sb-brand'><img src='{LOGO_URL}'><div class='sb-brand-name'>AutoCount<span style='color:#38bdf8;'>.ai</span></div></div>"
    f"<div class='sb-user'><div class='avatar'>{html.escape(_ini_user)}</div><div><div class='sb-user-name'>{html.escape(str(curr_user['nombre']))}</div>"
    f"<div class='sb-user-role'>{html.escape(str(curr_rol))}</div></div></div>", unsafe_allow_html=True)

if curr_rol == "SuperAdmin":
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT nit, razon_social FROM tenants")
    all_tenants = c.fetchall(); conn.close()
    tenant_opts = [f"{t[0]} - {t[1]}" for t in all_tenants]
    active_tenant_str = st.sidebar.selectbox("🏢 Panel SuperAdmin: Empresa Activa", tenant_opts)
    curr_tenant_nit = active_tenant_str.split(" - ")[0]
else: curr_tenant_nit = curr_user['tenant_nit']

if st.session_state.get('last_tenant_active') != curr_tenant_nit:
    st.cache_data.clear()
    st.session_state['last_tenant_active'] = curr_tenant_nit
    for key in list(st.session_state.keys()):
        if key not in ['authenticated_user', 'last_tenant_active']: del st.session_state[key]
    st.rerun()

curr_tenant = db_get_tenant(curr_tenant_nit)
auto_clean_processed_docs(curr_tenant_nit)

can_upload  = curr_rol in ['SuperAdmin', 'Administrador', 'Administrativo', 'Auxiliar Administrativo']
can_approve = curr_rol in ['SuperAdmin', 'Administrador', 'Administrativo']
can_cause   = curr_rol in ['SuperAdmin', 'Administrador', 'Asistente Contable']
can_config  = curr_rol in ['SuperAdmin']
can_admin   = curr_rol in ['SuperAdmin', 'Administrador']
can_treasury = curr_rol in ['SuperAdmin', 'Administrador', 'Asistente Contable']  # modifica pagos en Tesorería

# ==========================================
# 9. ESTRUCTURA PRINCIPAL (HEADER Y SIDEBAR)
# ==========================================
st.sidebar.markdown("<div class='sidebar-title'>Menú Principal</div>", unsafe_allow_html=True)
_menu_todas = [
    "🏠 Inicio (Dashboard)",
    "📥 1. Recepción & Aprobación", 
    "🏢 2. Causación Siigo (CXP)", 
    "💳 3. Causación Tarjetas", 
    "📄 4. Documentos Soporte (DS)", 
    "📦 5. Caja Menor",
    "📊 6. Tablero Audit (Ajustes)", 
    "📈 7. Reportes y Excel", 
    "💰 8. Tesorería (CXP & Pagos)",
    "⚙️ Configuración Empresa"
]
def _menu_visible(m):
    if any(k in m for k in ("2. Causación", "3. Causación", "4. Documentos Soporte")): return can_cause
    if "6. Tablero" in m: return can_cause or can_approve
    if "Configuración Empresa" in m: return can_admin
    return True
menu_opciones = [m for m in _menu_todas if _menu_visible(m)]
panel_seleccionado = st.sidebar.radio("Navegación:", menu_opciones)

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Sincronizar Maestros Siigo", use_container_width=True):
    st.cache_data.clear()
    maestros = cargar_maestros_siigo(curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'])
    st.sidebar.success("¡Sincronizado!")
else: maestros = cargar_maestros_siigo(curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'])

st.sidebar.caption(f"📊 **Maestros Cargados:** Terceros `{len(maestros.get('terceros', {}))}` | PUC `{len(curr_tenant.get('puc', []))}`")

if can_admin:
    st.sidebar.markdown("<div class='sidebar-title'>⚙️ Cargar PUC (Excel)</div>", unsafe_allow_html=True)
    excel_puc = st.sidebar.file_uploader("Sube tu archivo", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
    if excel_puc:
        file_key = f"{curr_tenant_nit}_{excel_puc.name}_{excel_puc.size}"
        if st.session_state.get('last_puc_key') != file_key:
            nuevos_puc = procesar_excel_puc(excel_puc)
            if nuevos_puc:
                conn = get_db_connection()
                conn.execute("UPDATE tenants SET puc=? WHERE nit=?", (json.dumps(nuevos_puc), curr_tenant_nit))
                conn.commit(); conn.close()
                st.session_state['last_puc_key'] = file_key
                curr_tenant['puc'] = nuevos_puc
                st.sidebar.success(f"✅ {len(nuevos_puc)} cuentas cargadas.")

st.sidebar.markdown("---")
with st.sidebar.expander("🔑 Cambiar mi contraseña"):
    with st.form("form_cambiar_pass", clear_on_submit=True):
        _pw_act = st.text_input("Contraseña actual", type="password")
        _pw_new = st.text_input("Nueva contraseña (mín. 6)", type="password")
        _pw_rep = st.text_input("Repetir nueva contraseña", type="password")
        if st.form_submit_button("Actualizar contraseña", use_container_width=True):
            if not db_auth_user(curr_user['email'], _pw_act): st.error("La contraseña actual no es correcta.")
            elif len(_pw_new) < 6: st.error("La nueva contraseña debe tener al menos 6 caracteres.")
            elif _pw_new != _pw_rep: st.error("Las contraseñas nuevas no coinciden.")
            else:
                _conn_pw = get_db_connection()
                _conn_pw.execute("UPDATE users SET password=? WHERE email=?", (hash_password(_pw_new, curr_user['email']), curr_user['email']))
                _conn_pw.commit(); _conn_pw.close()
                st.success("✅ Contraseña actualizada.")

if st.sidebar.button("🚪 Cerrar Sesión", use_container_width=True): 
    st.session_state['authenticated_user'] = None; st.rerun()

st.sidebar.markdown(f"<div class='sb-foot'>AutoCount.ai · v2.0<br>💾 Datos: {MODO_BD}<br>© 2026 Davinci Technologies</div>", unsafe_allow_html=True)

# ---- Barra superior (migas de pan + estado de Siigo) ----
_siigo_ok = not maestros.get("error")
st.markdown(
    '<div class="top-bar-container">'
    '<div class="top-bar-logo"><img src="' + LOGO_URL + '" alt="Logo"><div>'
    '<div class="top-bar-title">AutoCount<span style="color: #38bdf8;">.ai</span><small>SaaS Edition</small></div>'
    '<div class="crumb">🏢 ' + html.escape(str(curr_tenant['razon_social'])) + ' &nbsp;›&nbsp; ' + html.escape(str(panel_seleccionado)) + '</div>'
    '</div></div>'
    '<div class="top-bar-user">'
    + ('<span class="chip chip-ok">● Siigo conectado</span>' if _siigo_ok else '<span class="chip chip-bad">● Siigo sin conexión</span>')
    + '<span class="chip">📅 ' + datetime.now().strftime("%d/%m/%Y") + '</span>'
    '<div class="top-bar-who"><span class="avatar">' + html.escape(_ini_user) + '</span><div><b>' + html.escape(str(curr_user['nombre']))
    + '</b><span class="top-bar-user-badge">' + html.escape(str(curr_rol)) + '</span></div></div>'
    '</div></div>', unsafe_allow_html=True)

if st.session_state.pop('tema_aviso', False):
    st.warning("🎨 Se instaló el tema claro (archivo .streamlit/config.toml). Detén Streamlit (Ctrl+C) y vuelve a ejecutarlo UNA vez para que se aplique por completo.")

if not USANDO_POSTGRES and os.getcwd().startswith("/mount/src"):
    st.error("⚠️ Esta app está en la nube SIN base de datos persistente: todo lo que registres se BORRARÁ cuando el servidor se reinicie o se duerma. Configura el secreto DATABASE_URL (guía de despliegue).")

# ---- Aviso del último envío a Google Sheets ----
if st.session_state.get('webhook_resultado'):
    _wh = st.session_state.pop('webhook_resultado')
    if _wh.get("ok"):
        st.toast(f"📄 Google Sheets ({_wh.get('etiqueta','')}): {_wh.get('msg','')}", icon="✅")
        if _wh.get("aviso"): st.warning(f"⚠️ La fila llegó a Google Sheets ({_wh.get('etiqueta','')}), pero: {_wh['aviso']}")
    else: st.error(f"⚠️ NO se pudo enviar a Google Sheets ({_wh.get('etiqueta','')}): {_wh.get('msg','')}")

if can_admin:
    if st.sidebar.button("🧪 Probar conexión Google Sheets", use_container_width=True):
        try:
            _r = requests.get(WEBHOOK_SHEETS_URL, timeout=30, allow_redirects=True)
            st.sidebar.code(f"HTTP {_r.status_code}\n{_r.text[:200]}")
        except Exception as _e:
            st.sidebar.error(f"Error de red: {_e}")

# ==========================================
# 10. ENRUTADOR DE PANELES (ROUTING)
# ==========================================

# ----------------------------------------------------
# PANEL 1: RECEPCIÓN Y APROBACIÓN
# ----------------------------------------------------
if panel_seleccionado == "🏠 Inicio (Dashboard)":
    page_title("🏠 Panel de Control")
    st.caption(f"Resumen operativo de {curr_tenant['razon_social']} · actualizado {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    try: R = construir_resumen_dashboard(curr_tenant_nit)
    except Exception as e_dash:
        R = None
        st.error(f"No se pudo calcular el resumen: {e_dash}")

    if R:
        st.markdown(
            '<div class="pipe">'
            f'<div class="pipe-step pipe-1"><div class="l">1 · Por aprobar</div><div class="n">{R["pend_aprobacion"]}</div><div class="d">FC + DS en bandeja</div></div>'
            f'<div class="pipe-step pipe-2"><div class="l">2 · Por causar</div><div class="n">{R["por_causar"]}</div><div class="d">Aprobados sin enviar a Siigo</div></div>'
            f'<div class="pipe-step pipe-3"><div class="l">3 · Causados</div><div class="n">{R["causados_total"]}</div><div class="d">{R["causados_mes"]} este mes</div></div>'
            f'<div class="pipe-step pipe-4"><div class="l">4 · Pagado este mes</div><div class="n">&#36;{R["pagado_mes"]:,.0f}</div><div class="d">Tesorería</div></div>'
            '</div>', unsafe_allow_html=True)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("💰 Deuda activa", f"${R['deuda']:,.0f}")
        k2.metric("🔴 Vencido", f"${R['vencido']:,.0f}")
        k3.metric("🗓️ Programado a pagar", f"${R['programado']:,.0f}")
        k4.metric("📦 Caja menor (docs)", R['caja'])

        colores_aging = {"Vigente": "#22c55e", "1 a 30": "#facc15", "31 a 60": "#f97316", "61 a 90": "#ef4444", "Mayor a 90": "#991b1b"}
        col_a, col_b = st.columns(2)
        with col_a: st.markdown(render_barras_html("⏳ Antigüedad de la cartera", [(k, v, colores_aging[k]) for k, v in R['aging'].items()]), unsafe_allow_html=True)
        with col_b: st.markdown(render_barras_html("🏆 Top 5 proveedores por saldo", [(p, v, "#3b82f6") for p, v in R['top_prov']]), unsafe_allow_html=True)

        col_c, col_d = st.columns(2)
        with col_c:
            st.markdown("##### 📅 Vencimientos próximos (7 días)")
            if R['proximos']: st.dataframe(pd.DataFrame(R['proximos']), use_container_width=True, hide_index=True)
            else: st.info("Sin vencimientos en los próximos 7 días.")
        with col_d:
            st.markdown("##### ⚡ Últimas causaciones")
            if R['ultimas']: st.dataframe(pd.DataFrame(R['ultimas']), use_container_width=True, hide_index=True)
            else: st.info("Aún no hay documentos causados.")

elif panel_seleccionado == "📥 1. Recepción & Aprobación":
    page_title("📥 Recepción y Aprobación de Documentos")
    st.caption("Sube y clasifica los documentos para causación en Siigo (CXP/TC) o para tu reporte interno de Caja Menor.")
    terceros_dict, cc_lista = maestros.get("terceros", {}), maestros.get("centros_costo", [])
    cc_opciones = ["-- Sin Centro de Costo (Opcional) --"] + [c["nombre"] for c in cc_lista]

    subtab_fc, subtab_ds = st.tabs(["🧾 Facturas de Compra (FC)", "📄 Documentos Soporte (DS)"])

    with subtab_fc:
        if can_upload:
            with st.container(border=True):
                st.markdown("##### ⚡ Área de Carga Rápida (XML / ZIP)")
                if 'fc_up_key' not in st.session_state: st.session_state['fc_up_key'] = 0
                uploaded_fc = st.file_uploader("Arrastra aquí tus archivos XML o ZIP", type=["zip", "xml"], accept_multiple_files=True, key=f"up_fc_p1_{st.session_state['fc_up_key']}", label_visibility="collapsed")
                
                f_r1, f_r2, f_r3 = st.columns([1.6, 1.2, 1.2])
                usar_rango_fc = f_r1.checkbox("📅 Filtrar por fecha de emisión", value=True, key="rango_fc_on", help="Solo carga facturas emitidas dentro del rango. Las de fuera quedan retenidas y puedes cargarlas igual.")
                rango_desde_fc = f_r2.date_input("Desde", value=datetime.strptime(FECHA_MINIMA_RECEPCION, "%Y-%m-%d").date(), key="rango_fc_desde", disabled=not usar_rango_fc)
                rango_hasta_fc = f_r3.date_input("Hasta", value=datetime.now().date(), key="rango_fc_hasta", disabled=not usar_rango_fc)
                if usar_rango_fc and rango_desde_fc > rango_hasta_fc: st.warning("La fecha 'Desde' es posterior a 'Hasta': no se cargará ninguna factura.")

                c_btn1, c_btn2, c_btn3 = st.columns([2, 2, 1])
                with c_btn1: btn_manual_fc = st.button("🚀 Procesar Archivos Subidos", type="primary", use_container_width=True)
                with c_btn2: btn_drive_fc = st.button("☁️ Sincronizar Google Drive", type="secondary", use_container_width=True)
                with c_btn3: 
                    if can_admin and st.button("🧹 Limpiar", use_container_width=True):
                        _cx = get_db_connection(); _cx.execute("DELETE FROM docs WHERE tenant_nit=?", (curr_tenant_nit,)); _cx.commit(); _cx.close()
                        st.toast("🧹 Memoria borrada.", icon="✅"); st.rerun()

                data_list, stats_drive, origen_fc = [], {}, ""
                if btn_manual_fc and uploaded_fc:
                    origen_fc = "Archivos subidos"
                    for file in uploaded_fc: process_bytes(file.name, file.read(), data_list, tenant_nit=curr_tenant_nit)
                
                if btn_drive_fc:
                    origen_fc = "Google Drive"
                    url_api = _secret("DRIVE_FC_URL", "https://script.google.com/macros/s/AKfycbyyujzRVc6JsE--ENDSDiAMyIDNKJbDxbUirpTBXnc3KJxNI6HJfU7dJT9di97UTuzK/exec")
                    with st.spinner("Consultando Google Drive Nube..."):
                        exito_d, msg_d = extraer_facturas_desde_drive_cloud(url_api, data_list, tenant_nit=curr_tenant_nit, stats=stats_drive)
                        if not exito_d: st.error(msg_d)
                        else: st.info(msg_d)
                    if exito_d and stats_drive.get("archivos", 0) > 0 and not data_list:
                        st.warning(f"Drive devolvió {stats_drive['archivos']} archivo(s) ({stats_drive.get('detalle', '')}) pero ninguno produjo una factura. Pueden ser eventos de la DIAN (acuses), facturas emitidas a otro NIT, o archivos que no son XML/ZIP.")

                if data_list:
                    res_fc = guardar_lote_recepcion(curr_tenant_nit, data_list, "FC", usar_rango_fc, rango_desde_fc, rango_hasta_fc)
                    retener_fuera_de_rango("FC", res_fc["fuera"])
                    st.session_state['result_upload_fc'] = {"added": res_fc["added"], "skipped": res_fc["skipped"], "leidos": res_fc["leidos"], "n_fuera": len(res_fc["fuera"]), "origen": origen_fc}
                    st.session_state['fc_up_key'] += 1; st.rerun()

                mostrar_resultado_recepcion("FC")

        fc_sub_tab1, fc_sub_tab2 = st.tabs(["⏳ Revisiones Pendientes", "🚫 Documentos Rechazados"])
        with fc_sub_tab1:
            fc_pendientes = db_get_docs(curr_tenant_nit, "FC", "Pendiente")
            fc_pendientes = sorted(fc_pendientes, key=lambda x: str(x.get("Resumen", {}).get("Fecha", "")), reverse=True)
            
            if not fc_pendientes: st.info("No hay facturas pendientes en la bandeja.")
            else:
                curr_m = ""
                for idx_doc, f in enumerate(fc_pendientes):
                    r = f["Resumen"]
                    curr_m = render_month_header(curr_m, r.get("Fecha", ""))
                    
                    clean_nit = re.sub(r'\D', '', str(r["NIT"]))
                    esta_en_siigo = clean_nit in terceros_dict
                    with st.container(border=True):
                        c1, c2, c3, c4 = st.columns([2, 4, 2, 2])
                        with c1: st.markdown(f"### FC-{r['ID']}\n**{r['Fecha']}**")
                        with c2: 
                            st.markdown(f"#### {r['Proveedor']}"); st.caption(f"NIT: {clean_nit}")
                            if esta_en_siigo: st.markdown("<span class='badge-ok'>✅ Tercero Creado en Siigo</span>", unsafe_allow_html=True)
                            else:
                                st.markdown("<span class='badge-warn'>🔴 Tercero No Creado</span>", unsafe_allow_html=True)
                                if can_approve and st.button("➕ Crear en Siigo", key=f"btn_crea_t_fc_{idx_doc}"): modal_formulario_tercero(clean_nit, r['Proveedor'], curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'], es_extranjero=False)
                        with c3: st.metric("Total a Pagar (COP)", f"${r['Total']:,.2f}")
                        with c4:
                            if can_approve:
                                sel_cc = st.selectbox("Centro de Costo", options=cc_opciones, index=cc_opciones.index(r.get("CentroCosto")) if r.get("CentroCosto") in cc_opciones else 0, key=f"fc_cc_{idx_doc}", label_visibility="collapsed")
                                r["CentroCosto"] = None if sel_cc == "-- Sin Centro de Costo (Opcional) --" else sel_cc
                        
                        if can_approve:
                            st.markdown("---")
                            st.markdown("##### 📌 Destino y Clasificación (Tesorería)")
                            a1, a2, a3 = st.columns([2.5, 2, 3])
                            with a1: destino_doc = st.radio("Destino de la Factura:", ["🏢 Causar en Siigo (CXP)", "💳 Pago con Tarjeta de Crédito", "📦 Legalización Caja Menor"], horizontal=False, key=f"dest_fc_{idx_doc}")
                            with a2: clasif_teso = st.selectbox("Clasificación del Gasto:", OPCIONES_CLASIFICACION, index=0, key=f"clasif_fc_{idx_doc}")
                            with a3:
                                st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
                                c_btn1, c_btn2 = st.columns(2)
                                with c_btn1:
                                    if st.button("✅ Aprobar", key=f"btn_ap_fc_{idx_doc}", type="primary", use_container_width=True):
                                        estado_final = "Caja Menor" if "Caja Menor" in destino_doc else "Aprobado"
                                        clasif_final = "CXP" if "CXP" in destino_doc else ("Tarjeta" if "Tarjeta" in destino_doc else "Caja Menor")
                                        r["Estado"], r["Clasificacion"], r["Clasificacion_Teso"], r["UsuarioAprobador"], r["FechaAprobacion"] = estado_final, clasif_final, clasif_teso, curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, r['ID'], "FC", estado_final, f, r['NIT'])
                                        st.toast(f"✅ FC-{r['ID']} clasificada como {clasif_teso} y movida a {clasif_final}.", icon="🎉"); st.rerun()
                                with c_btn2:
                                    if st.button("❌ Rechazar", key=f"btn_rec_fc_{idx_doc}", use_container_width=True):
                                        r["Estado"], r["UsuarioAprobador"], r["FechaAprobacion"] = "Rechazado", curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, r['ID'], "FC", "Rechazado", f, r['NIT'])
                                        st.toast(f"🚫 FC-{r['ID']} Rechazada.", icon="🗑️"); st.rerun()
                                        
                        with st.expander(f"👁️ Ver Detalle de FC-{r['ID']}", expanded=False):
                            d_col1, d_col2 = st.columns([3, 1])
                            with d_col1: st.dataframe(pd.DataFrame(f["Detalle"]), use_container_width=True)
                            with d_col2:
                                if f.get("pdf_b64"): st.download_button("💾 Ver PDF", data=safe_b64decode(f["pdf_b64"]), file_name=f"Original_FC_{r['ID']}.pdf", mime="application/pdf", key=f"dl_fc_pdf_{idx_doc}")

        with fc_sub_tab2:
            fc_rechazadas = db_get_docs(curr_tenant_nit, "FC", "Rechazado")
            fc_rechazadas = sorted(fc_rechazadas, key=lambda x: str(x.get("Resumen", {}).get("Fecha", "")), reverse=True)
            
            if not fc_rechazadas: st.info("No hay facturas rechazadas.")
            else:
                curr_m = ""
                for idx_doc, f in enumerate(fc_rechazadas):
                    r = f["Resumen"]
                    curr_m = render_month_header(curr_m, r.get("Fecha", ""))
                    
                    with st.container(border=True):
                        c1, c2, c3, c4, c5 = st.columns([1.5, 3, 1.5, 1.5, 2])
                        with c1: st.markdown(f"**FC-{r['ID']}**")
                        with c2: st.markdown(f"**{r['Proveedor']}**"); st.caption(f"NIT: {r['NIT']}")
                        with c3: st.markdown(f"{r['Fecha']}")
                        with c4: st.markdown(f"**${r['Total']:,.2f}**")
                        with c5:
                            if can_approve and st.button("🔄 Regresar a Pendientes", key=f"btn_reopen_fc_{idx_doc}"):
                                r["Estado"] = "Pendiente"; db_save_doc(curr_tenant_nit, r['ID'], "FC", "Pendiente", f, r['NIT']); st.rerun()

    with subtab_ds:
        if can_upload:
            with st.container(border=True):
                st.markdown("##### ⚡ Área de Carga Rápida (PDF Cuentas de Cobro)")
                if 'ds_up_key' not in st.session_state: st.session_state['ds_up_key'] = 200
                uploaded_ds = st.file_uploader("Arrastra aquí tus archivos PDF", type=["pdf"], accept_multiple_files=True, key=f"up_ds_p1_{st.session_state['ds_up_key']}", label_visibility="collapsed")
                
                f_d1, f_d2, f_d3 = st.columns([1.6, 1.2, 1.2])
                usar_rango_ds = f_d1.checkbox("📅 Filtrar por fecha del documento", value=True, key="rango_ds_on", help="Solo carga documentos con fecha dentro del rango. Los de fuera quedan retenidos y puedes cargarlos igual.")
                rango_desde_ds = f_d2.date_input("Desde", value=datetime.strptime(FECHA_MINIMA_RECEPCION, "%Y-%m-%d").date(), key="rango_ds_desde", disabled=not usar_rango_ds)
                rango_hasta_ds = f_d3.date_input("Hasta", value=datetime.now().date(), key="rango_ds_hasta", disabled=not usar_rango_ds)
                if usar_rango_ds and rango_desde_ds > rango_hasta_ds: st.warning("La fecha 'Desde' es posterior a 'Hasta': no se cargará ningún documento.")

                c_btn_ds1, c_btn_ds2 = st.columns([1, 1])
                with c_btn_ds1: btn_manual_ds = st.button("🚀 Procesar Documentos (IA)", key="btn_proc_ds", type="primary", use_container_width=True)
                with c_btn_ds2: btn_drive_ds = st.button("☁️ Sincronizar Google Drive (IA)", type="secondary", use_container_width=True)
                
                nuevos_ds, origen_ds = [], ""
                if btn_manual_ds and uploaded_ds:
                    origen_ds = "Archivos subidos"
                    nuevos_ds = [extraer_datos_pdf_soporte(f.read(), f.name) for f in uploaded_ds]
                if btn_drive_ds:
                    origen_ds = "Google Drive"
                    url_api = _secret("DRIVE_DS_URL", "https://script.google.com/macros/s/AKfycbwsbar6jmdHl8xUhwJqR8OZo0C4Xk4TveU4iYzU0VPdxUCB9_lUE-xivSm0mn6bhHTpZw/exec")
                    with st.spinner("Procesando PDFs con IA desde Drive..."):
                        try:
                            res = requests.get(url_api, timeout=300)
                            if res.status_code == 200:
                                archivos = res.json()
                                if isinstance(archivos, list) and len(archivos) > 0:
                                    for item in archivos:
                                        fname, b64_str = item.get("filename", "documento.pdf"), item.get("base64", "")
                                        if b64_str and fname.lower().endswith(".pdf"): nuevos_ds.append(extraer_datos_pdf_soporte(safe_b64decode(b64_str), fname))
                                    st.info(f"Drive devolvió {len(archivos)} archivo(s); {len(nuevos_ds)} PDF procesado(s) con IA.")
                                else: st.info("No se encontraron archivos en Drive.")
                            else: st.error(f"Error Drive: {res.status_code}")
                        except Exception as e: st.error(f"Error IA: {e}")

                if nuevos_ds:
                    res_ds = guardar_lote_recepcion(curr_tenant_nit, nuevos_ds, "DS", usar_rango_ds, rango_desde_ds, rango_hasta_ds)
                    retener_fuera_de_rango("DS", res_ds["fuera"])
                    st.session_state['result_upload_ds'] = {"added": res_ds["added"], "skipped": res_ds["skipped"], "leidos": res_ds["leidos"], "n_fuera": len(res_ds["fuera"]), "origen": origen_ds}
                    st.session_state['ds_up_key'] += 1; st.rerun()

                mostrar_resultado_recepcion("DS")

        ds_sub_tab1, ds_sub_tab2 = st.tabs(["⏳ Revisiones Pendientes", "🚫 Documentos Rechazados"])
        with ds_sub_tab1:
            ds_pendientes = db_get_docs(curr_tenant_nit, "DS", "Pendiente")
            ds_pendientes = sorted(ds_pendientes, key=lambda x: str(x.get("fecha", "")), reverse=True)
            
            if not ds_pendientes: st.info("No hay Documentos Soporte pendientes.")
            else:
                curr_m = ""
                for idx_ds, d in enumerate(ds_pendientes):
                    curr_m = render_month_header(curr_m, d.get("fecha", ""))
                    
                    clean_nit = re.sub(r'\D', '', str(d["nit"]))
                    esta_en_siigo = clean_nit in terceros_dict
                    with st.container(border=True):
                        c1, c2, c3, c4 = st.columns([2, 4, 2, 2])
                        with c1: st.markdown(f"### DS-{d['documento_ref']}\n**{d['fecha']}**")
                        with c2: 
                            st.markdown(f"#### {d['proveedor']}"); st.caption(f"NIT: {clean_nit}")
                            if esta_en_siigo: st.markdown("<span class='badge-ok'>✅ Tercero Creado en Siigo</span>", unsafe_allow_html=True)
                            else:
                                st.markdown("<span class='badge-warn'>🔴 Tercero No Creado</span>", unsafe_allow_html=True)
                                if can_approve and st.button("➕ Crear en Siigo", key=f"btn_crea_t_ds_{idx_ds}"): modal_formulario_tercero(clean_nit, d['proveedor'], curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'], es_extranjero=(d['moneda_origen'] == "USD"))
                        with c3: st.metric("Total a Pagar", f"{'$' if d['moneda_origen'] == 'COP' else 'USD $'}{d['monto_origen']:,.2f}")
                        with c4:
                            if can_approve:
                                sel_cc = st.selectbox("Centro de Costo", options=cc_opciones, index=cc_opciones.index(d.get("centro_costo")) if d.get("centro_costo") in cc_opciones else 0, key=f"ds_cc_{idx_ds}", label_visibility="collapsed")
                                d["centro_costo"] = None if sel_cc == "-- Sin Centro de Costo (Opcional) --" else sel_cc
                        
                        if can_approve:
                            st.markdown("---")
                            st.markdown("##### 📌 Destino y Clasificación (Tesorería)")
                            a1, a2, a3 = st.columns([2.5, 2, 3])
                            with a1: destino_doc = st.radio("Destino del Documento:", ["🏢 Causar en Siigo (CXP)", "💳 Pago con Tarjeta de Crédito", "📦 Legalización Caja Menor"], horizontal=False, key=f"dest_ds_{idx_ds}")
                            with a2: clasif_teso = st.selectbox("Clasificación del Gasto:", OPCIONES_CLASIFICACION, index=0, key=f"clasif_ds_{idx_ds}")
                            with a3:
                                st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
                                c_btn1, c_btn2 = st.columns(2)
                                with c_btn1:
                                    if st.button("✅ Aprobar", key=f"btn_ap_ds_{idx_ds}", type="primary", use_container_width=True):
                                        estado_final = "Caja Menor" if "Caja Menor" in destino_doc else "Aprobado"
                                        clasif_final = "CXP" if "CXP" in destino_doc else ("Tarjeta" if "Tarjeta" in destino_doc else "Caja Menor")
                                        d["estado"], d["Clasificacion"], d["Clasificacion_Teso"], d["UsuarioAprobador"], d["FechaAprobacion"] = estado_final, clasif_final, clasif_teso, curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", estado_final, d, d['nit'])
                                        st.toast(f"✅ DS-{d['documento_ref']} movido a {clasif_final}.", icon="🎉"); st.rerun()
                                with c_btn2:
                                    if st.button("❌ Rechazar", key=f"btn_rec_ds_{idx_ds}", use_container_width=True):
                                        d["estado"], d["UsuarioAprobador"], d["FechaAprobacion"] = "Rechazado", curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", "Rechazado", d, d['nit'])
                                        st.toast(f"🚫 DS-{d['documento_ref']} Rechazado.", icon="🗑️"); st.rerun()
                                        
                        with st.expander(f"👁️ Ver PDF Extraído de DS-{d['documento_ref']}", expanded=False):
                            if d.get("pdf_b64"): st.download_button("💾 Ver PDF", data=safe_b64decode(d["pdf_b64"]), file_name=f"Original_DS_{d['documento_ref']}.pdf", mime="application/pdf", key=f"dl_ds_pdf_p1_{idx_ds}")
                            else: st.info("No hay PDF adjunto.")

        with ds_sub_tab2:
            ds_rechazados = db_get_docs(curr_tenant_nit, "DS", "Rechazado")
            ds_rechazados = sorted(ds_rechazados, key=lambda x: str(x.get("fecha", "")), reverse=True)
            
            if not ds_rechazados: st.info("No hay Documentos Soporte rechazados.")
            else:
                curr_m = ""
                for idx_ds, d in enumerate(ds_rechazados):
                    curr_m = render_month_header(curr_m, d.get("fecha", ""))
                    
                    with st.container(border=True):
                        c1, c2, c3, c4, c5 = st.columns([1.5, 3, 1.5, 1.5, 2])
                        with c1: st.markdown(f"**DS-{d['documento_ref']}**")
                        with c2: st.markdown(f"**{d['proveedor']}**"); st.caption(f"NIT: {d['nit']}")
                        with c3: st.markdown(f"{d['fecha']}")
                        with c4: st.markdown(f"**{'$' if d['moneda_origen'] == 'COP' else 'USD $'}{d['monto_origen']:,.2f}**")
                        with c5:
                            if can_approve and st.button("🔄 Regresar a Pendientes", key=f"btn_reopen_ds_{idx_ds}"):
                                d["estado"] = "Pendiente"; db_save_doc(curr_tenant_nit, d['documento_ref'], "DS", "Pendiente", d, d['nit']); st.rerun()

# ----------------------------------------------------
# PANEL 2: CAUSACIÓN CXP (FACTURAS PROVEEDORES)
# ----------------------------------------------------
elif panel_seleccionado == "🏢 2. Causación Siigo (CXP)":
    page_title("🏢 Causación de Facturas a Crédito (CXP)")
    if not can_cause: st.warning("🔒 No tienes permisos para causar en Siigo.")
    else:
        todos_aprobados = db_get_docs(curr_tenant_nit, "FC", "Aprobado")
        fc_aprobadas = [d for d in todos_aprobados if d["Resumen"].get("Clasificacion") == "CXP"]
        fc_aprobadas = sorted(fc_aprobadas, key=lambda x: str(x.get("Resumen", {}).get("Fecha", "")), reverse=True)
        
        if not fc_aprobadas: st.info("🎉 Excelente, no hay facturas de CXP pendientes por causar.")
        else:
            terceros_lista, cc_lista = maestros.get("terceros_lista", []), maestros.get("centros_costo", [])
            types_fc, pagos_lista, prods_lista = maestros.get("doc_types_fc", [{"id": 19147, "nombre": "FC - 1 - Compra (ID: 19147)"}]), maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}]), maestros.get("productos", [])
            list_iva, list_rete, list_reteiva, list_ica = maestros.get("impuestos_iva", []), maestros.get("impuestos_rete", []), maestros.get("impuestos_reteiva", []), maestros.get("impuestos_ica", [])

            curr_m = ""
            for idx_doc, doc in enumerate(fc_aprobadas):
                r = doc["Resumen"]
                curr_m = render_month_header(curr_m, r.get("Fecha", ""))
                
                llave_factura = f"cxp_{r['ID']}_{idx_doc}"
                with st.container(border=True):
                    c_head1, c_head2 = st.columns([4, 1])
                    with c_head1:
                        with st.expander(f"👁️ Ver Detalle de Factura {r['ID']} ({r['Proveedor']})", expanded=False): st.dataframe(pd.DataFrame(doc["Detalle"]), use_container_width=True)
                    with c_head2:
                        if doc.get("pdf_b64"): st.download_button("💾 PDF Factura", data=safe_b64decode(doc["pdf_b64"]), file_name=f"Factura_{r['ID']}.pdf", mime="application/pdf", key=f"dl_fac_pdf_{llave_factura}")
                    
                    e1, e2, e3, e4 = st.columns([2, 1.8, 1.8, 1.5])
                    with e1:
                        dt_sel = st.selectbox("Tipo de Documento", options=[t["nombre"] for t in types_fc], key=f"fc_dt_{llave_factura}")
                        id_type_fc = next((t["id"] for t in types_fc if t["nombre"] == dt_sel), 19147)
                        idx_terc_def = buscar_indice_tercero(r["Proveedor"], r["NIT"], terceros_lista)
                        opciones_terc = terceros_lista.copy()
                        if idx_terc_def < 0: opciones_terc.insert(0, f"⚠️ TERCERO NO CREADO EN SIIGO ({r['NIT']})"); tercero_sel = st.selectbox("Proveedor", options=opciones_terc, index=0, key=f"fc_terc_{llave_factura}")
                        else: tercero_sel = st.selectbox("Proveedor", options=opciones_terc, index=idx_terc_def, key=f"fc_terc_{llave_factura}")
                        nit_ingresado = tercero_sel.split(" - ")[0].strip() if "⚠️" not in tercero_sel else r["NIT"]
                    with e2:
                        fecha_fac = st.text_input("Fecha Factura", value=r["Fecha"], key=f"fc_fec_{llave_factura}")
                        num_fac = st.text_input("Consecutivo Proveedor", value=re.sub(r'\D', '', str(r['ID'])), key=f"fc_num_{llave_factura}")
                    with e3:
                        cc_opts = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
                        cc_header_sel = st.selectbox("Centro de costo Global", options=cc_opts, index=cc_opts.index(r.get("CentroCosto")) if r.get("CentroCosto") in cc_opts else 0, key=f"fc_cc_head_{llave_factura}")
                        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_header_sel), None) if cc_header_sel != "-- Sin Centro de Costo --" else None
                    with e4: st.metric("Total Neto XML", f"${r['Total']:,.0f}")

                    st.markdown("<div class='siigo-table-header'># | Tipo | Código / Producto | Descripción | Cant | Vr. Unitario | Imp. Cargo (IVA) | Imp. Retención | Valor Total | Acciones</div>", unsafe_allow_html=True)
                    items_siigo, acum_subtotal, acum_iva = [], 0.0, 0.0
                    for item_idx, item in enumerate(doc.get("Detalle", [])):
                        i0, i1, i2, i3, i4, i5, i6, i7, i8, i9 = st.columns([0.3, 0.8, 1.8, 1.7, 0.5, 1.0, 1.3, 1.7, 0.9, 0.4])
                        with i0: st.markdown(f"**{item_idx+1}**")
                        with i1: tipo_item = st.selectbox("Tipo", options=["Account", "Product"], index=0, key=f"fc_tp_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i2:
                            if tipo_item == "Account": puc_sel = st.selectbox("Código PUC", options=curr_tenant.get('puc', DEFAULT_PUC), index=0, key=f"puc_sel_{llave_factura}_{item_idx}", label_visibility="collapsed"); code_item = re.sub(r'[^\d]', '', puc_sel.split(" - ")[0].strip())
                            else: prod_sel = st.selectbox("Producto", options=prods_lista if prods_lista else ["Sin Productos"], key=f"fc_prod_{llave_factura}_{item_idx}", label_visibility="collapsed"); code_item = prod_sel.split(" - ")[0].strip()
                        with i3: desc_val = st.text_input("Descripción", value=item['Concepto'], key=f"fc_desc_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i4: cant_val = st.number_input("Cant", value=float(item.get('Cantidad', 1.0)), key=f"fc_cant_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i5: monto_val = st.number_input("Vr. Unitario", value=float(item['Subtotal']), key=f"fc_val_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i6:
                            iva_sel = st.selectbox("Imp. Cargo", options=[i["nombre"] for i in list_iva], index=buscar_indice_iva(item.get("IVA %", 0), list_iva), key=f"fc_iva_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_iva, pct_iva_sel = next((i["id"] for i in list_iva if i["nombre"] == iva_sel), 0), next((i["porcentaje"] for i in list_iva if i["nombre"] == iva_sel), 0.0)
                        with i7:
                            rete_sel = st.selectbox("Imp. Rete", options=[i["nombre"] for i in list_rete], key=f"fc_rete_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_rete = next((i["id"] for i in list_rete if i["nombre"] == rete_sel), 0)
                        with i8:
                            sub_row = round(monto_val * cant_val, 2); iva_row = round(sub_row * (pct_iva_sel / 100.0), 2); st.markdown(f"**${sub_row + iva_row:,.0f}**")
                        with i9:
                            if len(doc.get("Detalle", [])) > 1 and st.button("🗑️", key=f"btn_del_line_fc_{llave_factura}_{item_idx}"): doc["Detalle"].pop(item_idx); db_save_doc(curr_tenant_nit, r['ID'], "FC", "Aprobado", doc, r['NIT']); st.rerun()
                        acum_subtotal += sub_row; acum_iva += iva_row
                        items_siigo.append({"code": code_item, "type": tipo_item, "description": desc_val, "quantity": cant_val, "price": monto_val, "cost_center": id_cc_head, "id_iva": id_iva, "id_rete": id_rete})

                    if st.button("➕ Agregar Línea Adicional", key=f"btn_add_line_fc_{llave_factura}"): doc["Detalle"].append({"Concepto": "Línea Adicional", "Cantidad": 1.0, "Subtotal": 0.0, "IVA %": 0.0, "Valor IVA": 0.0}); db_save_doc(curr_tenant_nit, r['ID'], "FC", "Aprobado", doc, r['NIT']); st.rerun()

                    st.markdown("---")
                    b1, b2, b3 = st.columns([2, 1.8, 1.8])
                    with b1:
                        pago_sel = st.selectbox("Forma de pago en Siigo", options=[p["nombre"] for p in pagos_lista], key=f"fc_pago_{llave_factura}")
                        id_pago = next((p["id"] for p in pagos_lista if p["nombre"] == pago_sel), 1)
                    with b2:
                        sel_reteiva = st.selectbox("ReteIVA (Global)", options=[i["nombre"] for i in list_reteiva], key=f"fc_glob_reteiva_{llave_factura}")
                        id_reteiva = next((i["id"] for i in list_reteiva if i["nombre"] == sel_reteiva), 0)
                    with b3:
                        sel_reteica = st.selectbox("ReteICA (Global)", options=[i["nombre"] for i in list_ica], key=f"fc_glob_reteica_{llave_factura}")
                        id_reteica = next((i["id"] for i in list_ica if i["nombre"] == sel_reteica), 0)

                    total_neto_calculado = acum_subtotal + acum_iva
                    st.markdown(f"### **Total Transacción: ${total_neto_calculado:,.2f} COP**")

                    c_act1, c_act2 = st.columns([3, 1])
                    with c_act1:
                        if st.button(f"🚀 Generar CXP en Siigo", key=f"btn_fc_send_{llave_factura}", type="primary", use_container_width=True):
                            if "⚠️" in tercero_sel: st.error("🔴 Selecciona un tercero válido.")
                            else:
                                num_fac_clean = re.sub(r'\D', '', str(num_fac)) or "1"
                                valid_items = [it for it in items_siigo if it["price"] > 0 or len(items_siigo) == 1]
                                final_items_payload, val_retenciones_calc, ret_desglose = [], 0.0, {}
                                
                                for idx_item, it in enumerate(valid_items):
                                    sub_lin = it["quantity"] * it["price"]
                                    taxes_list = []
                                    if it["id_iva"] and int(it["id_iva"]) > 0: taxes_list.append({"id": int(it["id_iva"])})
                                    if it["id_rete"] and int(it["id_rete"]) > 0: 
                                        taxes_list.append({"id": int(it["id_rete"])})
                                        pct_rete = next((float(i["porcentaje"]) for i in list_rete if i["id"] == int(it["id_rete"])), 0)
                                        nom_rete = next((i["nombre"] for i in list_rete if i["id"] == int(it["id_rete"])), f"Retencion {pct_rete}%")
                                        val_r = sub_lin * (pct_rete/100.0); val_retenciones_calc += val_r; ret_desglose[nom_rete] = ret_desglose.get(nom_rete, 0) + val_r
                                    item_dict = {"code": it["code"], "type": it["type"], "description": it["description"], "quantity": it["quantity"], "price": it["price"], "taxes": taxes_list}
                                    if it["cost_center"]: item_dict["cost_center"] = it["cost_center"]
                                    final_items_payload.append(item_dict)

                                if id_reteiva and int(id_reteiva) > 0:
                                    pct_rete = next((float(i["porcentaje"]) for i in list_reteiva if i["id"] == int(id_reteiva)), 0); val_r = acum_iva * (pct_rete/100.0); val_retenciones_calc += val_r; ret_desglose[sel_reteiva] = ret_desglose.get(sel_reteiva, 0) + val_r
                                    
                                if id_reteica and int(id_reteica) > 0:
                                    pct_rete = next((float(i["porcentaje"]) for i in list_ica if i["id"] == int(id_reteica)), 0); val_r = acum_subtotal * (pct_rete/100.0); val_retenciones_calc += val_r; ret_desglose[sel_reteica] = ret_desglose.get(sel_reteica, 0) + val_r

                                for idx_item, it in enumerate(items_siigo):
                                    if it["id_rete"] and int(it["id_rete"]) > 0: doc["Detalle"][idx_item]["Retencion_Nombre"] = next((i["nombre"] for i in list_rete if i["id"] == int(it["id_rete"])), "Rete")
                                    else: doc["Detalle"][idx_item]["Retencion_Nombre"] = "0%"
                                    doc["Detalle"][idx_item]["Cta_PUC"] = it["code"]

                                retentions_payload = []
                                if id_reteiva and int(id_reteiva) > 0: retentions_payload.append({"id": int(id_reteiva)})
                                if id_reteica and int(id_reteica) > 0: retentions_payload.append({"id": int(id_reteica)})

                                total_pagar_final = total_neto_calculado - val_retenciones_calc
                                payload_fc = {"document": {"id": id_type_fc}, "date": fecha_fac, "supplier": {"identification": nit_ingresado, "branch_office": 0}, "retentions": retentions_payload, "observations": f"Causación AutoCount.ai - CXP Doc {num_fac_clean}", "items": final_items_payload, "payments": [{"id": id_pago, "value": round(total_pagar_final, 2), "due_date": fecha_fac}], "provider_invoice": {"prefix": "FC", "number": int(str(num_fac_clean)[:9])}}
                                if id_cc_head: payload_fc["cost_center"] = id_cc_head

                                exito, msg, doc_id_siigo, doc_num_siigo, real_total_pagar = causar_en_siigo_api(payload_fc, False, curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'])
                                if exito:
                                    doc["Resumen"]["Subtotal"] = acum_subtotal; doc["Resumen"]["IVA"] = acum_iva; doc["Resumen"]["Retenciones"] = round(total_neto_calculado - real_total_pagar, 2)
                                    doc["Resumen"]["Retenciones_Desglose"] = ret_desglose; doc["Resumen"]["FormaPago"] = pago_sel; doc["Resumen"]["TotalPagar"] = real_total_pagar; doc["Resumen"]["Total"] = total_neto_calculado
                                    doc["UsuarioCausador"], doc["FechaCausacion"] = curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    
                                    db_save_history(curr_tenant_nit, r['ID'], "FC", fecha_fac, total_neto_calculado, "COP", f"{doc_num_siigo}|||{doc_id_siigo}", r["Proveedor"], nit_ingresado, doc.get("pdf_b64"), json.dumps(doc), curr_user["email"])
                                    
                                    concepto_teso = " | ".join([str(i.get("Concepto", "")) for i in doc.get("Detalle", [])]) or "Factura de Compra"
                                    cc_teso = doc.get("Resumen", {}).get("CentroCosto", "-- Sin Centro de Costo --")
                                    clasif_t = doc.get("Resumen", {}).get("Clasificacion_Teso", "Proveedor")
                                    raw_teso = {
                                        "Empresa": curr_tenant['razon_social'],
                                        "Fecha Recibido": r.get("Fecha", datetime.now().strftime("%Y-%m-%d")),
                                        "Fecha Vto": r.get("FechaVencimiento", r.get("Fecha", datetime.now().strftime("%Y-%m-%d"))),
                                        "Nit": nit_ingresado,
                                        "Proveedor": r["Proveedor"],
                                        "No. documento": str(num_fac_clean),
                                        "Concepto": concepto_teso,
                                        "Centro de Costo": cc_teso,
                                        "Valor con Iva": total_neto_calculado,
                                        "Clasificacion": clasif_t,
                                        "Estado": "Por pagar",
                                        "Valor a Pagar - Vr fra USD": real_total_pagar
                                    }
                                    raw_teso["_destino"] = "CXP"
                                    db_save_treasury(curr_tenant_nit, str(num_fac_clean), r["Proveedor"], nit_ingresado, r["Fecha"], r.get("FechaVencimiento", r["Fecha"]), concepto_teso, cc_teso, total_neto_calculado, real_total_pagar, "Por pagar", clasif_t, raw_data=json.dumps(raw_teso))
                                    
                                    # 🔔 WEBHOOK GOOGLE SHEETS (CXP)
                                    _adj_cxp = armar_adjuntos_webhook({"id_doc_prov": r["ID"], "tipo": "FC", "fecha": fecha_fac, "total": total_neto_calculado, "moneda": "COP", "id_siigo_num": f"{doc_num_siigo}|||{doc_id_siigo}", "proveedor": r["Proveedor"], "nit": nit_ingresado, "data_json": json.dumps(doc), "usuario": curr_user["email"]}, curr_tenant)
                                    enviar_fila_webhook(construir_fila_webhook(
                                        "CXP", curr_tenant, curr_user["email"], "FC", num_fac_clean, doc_num_siigo,
                                        r["Proveedor"], nit_ingresado, fecha_fac, r.get("FechaVencimiento", fecha_fac),
                                        "COP", 1.0, cc_teso, concepto_teso, acum_subtotal, acum_iva,
                                        round(total_neto_calculado - real_total_pagar, 2), total_neto_calculado,
                                        real_total_pagar, pago_sel, clasif_t, "Por pagar"), "CXP", adjuntos=_adj_cxp)
                                    
                                    st.toast(f"✅ CXP Causada exitosamente: {doc_num_siigo}", icon="🎉"); st.rerun()
                                else: st.error(msg)
                    with c_act2:
                        if st.button("🔄 A Pendientes", key=f"btn_ret_cxp_{llave_factura}", use_container_width=True):
                            doc["Estado"], doc["Clasificacion"] = "Pendiente", ""
                            db_save_doc(curr_tenant_nit, r['ID'], "FC", "Pendiente", doc, r['NIT']); st.rerun()

# ----------------------------------------------------
# PANEL 3: CAUSACIÓN TARJETAS DE CRÉDITO
# ----------------------------------------------------
elif panel_seleccionado == "💳 3. Causación Tarjetas":
    page_title("💳 Causación de Pagos con Tarjeta de Crédito")
    if not can_cause: st.warning("🔒 No tienes permisos para causar en Siigo.")
    else:
        st.caption("Documentos clasificados para cruce y pago inmediato con T.C.")
        todos_aprobados = db_get_docs(curr_tenant_nit, "FC", "Aprobado")
        tc_aprobadas = [d for d in todos_aprobados if d["Resumen"].get("Clasificacion") == "Tarjeta"]
        tc_aprobadas = sorted(tc_aprobadas, key=lambda x: str(x.get("Resumen", {}).get("Fecha", "")), reverse=True)
        
        if not tc_aprobadas: st.info("🎉 No hay facturas de Tarjeta de Crédito pendientes por causar.")
        else:
            terceros_lista, cc_lista = maestros.get("terceros_lista", []), maestros.get("centros_costo", [])
            types_fc, pagos_lista, prods_lista = maestros.get("doc_types_fc", [{"id": 19147, "nombre": "FC - 1 - Compra (ID: 19147)"}]), maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}]), maestros.get("productos", [])
            list_iva, list_rete, list_reteiva, list_ica = maestros.get("impuestos_iva", []), maestros.get("impuestos_rete", []), maestros.get("impuestos_reteiva", []), maestros.get("impuestos_ica", [])
            
            curr_m = ""
            for idx_doc, doc in enumerate(tc_aprobadas):
                r = doc["Resumen"]
                curr_m = render_month_header(curr_m, r.get("Fecha", ""))
                
                llave_factura = f"tc_{r['ID']}_{idx_doc}"
                with st.container(border=True):
                    c_head1, c_head2 = st.columns([4, 1])
                    with c_head1:
                        with st.expander(f"👁️ Ver Detalle T.C. - Fac. {r['ID']} ({r['Proveedor']})", expanded=False): st.dataframe(pd.DataFrame(doc["Detalle"]), use_container_width=True)
                    with c_head2:
                        if doc.get("pdf_b64"): st.download_button("💾 PDF Original", data=safe_b64decode(doc["pdf_b64"]), file_name=f"Tarjeta_{r['ID']}.pdf", mime="application/pdf", key=f"dl_fac_pdf_{llave_factura}")
                    
                    e1, e2, e3, e4 = st.columns([2, 1.8, 1.8, 1.5])
                    with e1:
                        dt_sel = st.selectbox("Tipo de Comprobante", options=[t["nombre"] for t in types_fc], key=f"fc_dt_{llave_factura}")
                        id_type_fc = next((t["id"] for t in types_fc if t["nombre"] == dt_sel), 19147)
                        idx_terc_def = buscar_indice_tercero(r["Proveedor"], r["NIT"], terceros_lista)
                        opciones_terc = terceros_lista.copy()
                        if idx_terc_def < 0: opciones_terc.insert(0, f"⚠️ TERCERO NO CREADO EN SIIGO ({r['NIT']})"); tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=0, key=f"fc_terc_{llave_factura}")
                        else: tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=idx_terc_def, key=f"fc_terc_{llave_factura}")
                        nit_ingresado = tercero_sel.split(" - ")[0].strip() if "⚠️" not in tercero_sel else r["NIT"]
                    with e2:
                        fecha_fac = st.text_input("Fecha", value=r["Fecha"], key=f"fc_fec_{llave_factura}")
                        num_fac = st.text_input("No. Factura", value=re.sub(r'\D', '', str(r['ID'])), key=f"fc_num_{llave_factura}")
                    with e3:
                        cc_opts = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
                        cc_header_sel = st.selectbox("Centro de costo", options=cc_opts, index=cc_opts.index(r.get("CentroCosto")) if r.get("CentroCosto") in cc_opts else 0, key=f"fc_cc_head_{llave_factura}")
                        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_header_sel), None) if cc_header_sel != "-- Sin Centro de Costo --" else None
                    with e4: st.metric("Total Neto XML", f"${r['Total']:,.0f}")

                    st.markdown("<div class='siigo-table-header'># | Tipo | Código PUC | Descripción | Cant | Vr. Unitario | Imp. Cargo (IVA) | Imp. Retención | Valor Total</div>", unsafe_allow_html=True)
                    items_siigo, acum_subtotal, acum_iva = [], 0.0, 0.0
                    for item_idx, item in enumerate(doc.get("Detalle", [])):
                        i0, i1, i2, i3, i4, i5, i6, i7, i8 = st.columns([0.3, 0.8, 1.8, 1.7, 0.5, 1.0, 1.3, 1.7, 0.9])
                        with i0: st.markdown(f"**{item_idx+1}**")
                        with i1: tipo_item = st.selectbox("Tipo", options=["Account", "Product"], index=0, key=f"fc_tp_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i2:
                            cat_puc = curr_tenant.get('puc', DEFAULT_PUC)
                            puc_sel = st.selectbox("Código PUC", options=cat_puc, index=0, key=f"puc_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            code_item = re.sub(r'[^\d]', '', puc_sel.split(" - ")[0].strip())
                        with i3: desc_val = st.text_input("Descripción", value=item['Concepto'], key=f"fc_desc_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i4: cant_val = st.number_input("Cant", value=float(item.get('Cantidad', 1.0)), key=f"fc_cant_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i5: monto_val = st.number_input("Vr. Unitario", value=float(item['Subtotal']), key=f"fc_val_{llave_factura}_{item_idx}", label_visibility="collapsed")
                        with i6:
                            iva_sel = st.selectbox("Imp. Cargo", options=[i["nombre"] for i in list_iva], index=buscar_indice_iva(item.get("IVA %", 0), list_iva), key=f"fc_iva_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_iva, pct_iva_sel = next((i["id"] for i in list_iva if i["nombre"] == iva_sel), 0), next((i["porcentaje"] for i in list_iva if i["nombre"] == iva_sel), 0.0)
                        with i7:
                            rete_sel = st.selectbox("Imp. Retención", options=[i["nombre"] for i in list_rete], key=f"fc_rete_sel_{llave_factura}_{item_idx}", label_visibility="collapsed")
                            id_rete = next((i["id"] for i in list_rete if i["nombre"] == rete_sel), 0)
                        with i8:
                            sub_row = round(monto_val * cant_val, 2); iva_row = round(sub_row * (pct_iva_sel / 100.0), 2); st.markdown(f"**${sub_row + iva_row:,.0f}**")

                        acum_subtotal += sub_row; acum_iva += iva_row
                        items_siigo.append({"code": code_item, "type": tipo_item, "description": desc_val, "quantity": cant_val, "price": monto_val, "cost_center": id_cc_head, "id_iva": id_iva, "id_rete": id_rete})

                    st.markdown("---")
                    b1, b2, b3 = st.columns([2, 1.8, 1.8])
                    with b1:
                        pago_sel = st.selectbox("Cuenta Tarjeta de Crédito", options=[p["nombre"] for p in pagos_lista], key=f"fc_pago_{llave_factura}")
                        id_pago = next((p["id"] for p in pagos_lista if p["nombre"] == pago_sel), 1)
                    
                    total_neto_calculado = acum_subtotal + acum_iva
                    st.markdown(f"### **Total Transacción T.C.: ${total_neto_calculado:,.2f} COP**")

                    c_act1, c_act2 = st.columns([3, 1])
                    with c_act1:
                        if st.button(f"🚀 Procesar Pago T.C. en Siigo", key=f"btn_fc_send_{llave_factura}", type="primary", use_container_width=True):
                            if "⚠️" in tercero_sel: st.error("🔴 Selecciona un tercero válido.")
                            else:
                                num_fac_clean = re.sub(r'\D', '', str(num_fac)) or "1"
                                valid_items = [it for it in items_siigo if it["price"] > 0 or len(items_siigo) == 1]
                                final_items_payload = []
                                for idx_item, it in enumerate(valid_items):
                                    taxes_list = []
                                    if it["id_iva"] and int(it["id_iva"]) > 0: taxes_list.append({"id": int(it["id_iva"])})
                                    if it["id_rete"] and int(it["id_rete"]) > 0: taxes_list.append({"id": int(it["id_rete"])})
                                    item_dict = {"code": it["code"], "type": it["type"], "description": it["description"], "quantity": it["quantity"], "price": it["price"], "taxes": taxes_list}
                                    if it["cost_center"]: item_dict["cost_center"] = it["cost_center"]
                                    final_items_payload.append(item_dict)

                                payload_fc = {"document": {"id": id_type_fc}, "date": fecha_fac, "supplier": {"identification": nit_ingresado, "branch_office": 0}, "observations": f"Pago Tarjeta Crédito - AutoCount.ai - Doc {num_fac_clean}", "items": final_items_payload, "payments": [{"id": id_pago, "value": round(total_neto_calculado, 2), "due_date": fecha_fac}], "provider_invoice": {"prefix": "FC", "number": int(str(num_fac_clean)[:9])}}
                                if id_cc_head: payload_fc["cost_center"] = id_cc_head

                                exito, msg, doc_id_siigo, doc_num_siigo, real_total_pagar = causar_en_siigo_api(payload_fc, False, curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'])
                                if exito:
                                    doc["Resumen"]["Subtotal"] = acum_subtotal; doc["Resumen"]["IVA"] = acum_iva; doc["Resumen"]["Retenciones"] = 0.0
                                    doc["Resumen"]["FormaPago"] = pago_sel; doc["Resumen"]["TotalPagar"] = real_total_pagar; doc["Resumen"]["Total"] = total_neto_calculado
                                    doc["UsuarioCausador"], doc["FechaCausacion"] = curr_user["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    db_save_history(curr_tenant_nit, r['ID'], "FC", fecha_fac, total_neto_calculado, "COP", f"{doc_num_siigo}|||{doc_id_siigo}", r["Proveedor"], nit_ingresado, doc.get("pdf_b64"), json.dumps(doc), curr_user["email"])
                                    
                                    # 🔥 ENVIAMOS A TESORERÍA CON ESTRUCTURA EXACTA (Marcado como Pagado por TC)
                                    concepto_teso = " | ".join([str(i.get("Concepto", "")) for i in doc.get("Detalle", [])]) or "Factura de Compra"
                                    cc_teso = doc.get("Resumen", {}).get("CentroCosto", "-- Sin Centro de Costo --")
                                    clasif_t = doc.get("Resumen", {}).get("Clasificacion_Teso", "Proveedor")
                                    raw_teso = {
                                        "Empresa": curr_tenant['razon_social'],
                                        "Fecha Recibido": r.get("Fecha", datetime.now().strftime("%Y-%m-%d")),
                                        "Fecha Vto": r.get("FechaVencimiento", r.get("Fecha", datetime.now().strftime("%Y-%m-%d"))),
                                        "Nit": nit_ingresado,
                                        "Proveedor": r["Proveedor"],
                                        "No. documento": str(num_fac_clean),
                                        "Concepto": concepto_teso,
                                        "Centro de Costo": cc_teso,
                                        "Valor con Iva": total_neto_calculado,
                                        "Clasificacion": clasif_t,
                                        "Estado": "Pagado",
                                        "Valor a Pagar - Vr fra USD": real_total_pagar,
                                        "Banco Girador": "Tarjeta de Crédito",
                                        "Fecha de Pago": r.get("Fecha", datetime.now().strftime("%Y-%m-%d"))
                                    }
                                    raw_teso["_destino"] = "Tarjeta"
                                    db_save_treasury(curr_tenant_nit, str(num_fac_clean), r["Proveedor"], nit_ingresado, r["Fecha"], r.get("FechaVencimiento", r["Fecha"]), concepto_teso, cc_teso, total_neto_calculado, real_total_pagar, "Pagado", clasif_t, fecha_pago=r["Fecha"], banco="Tarjeta de Crédito", raw_data=json.dumps(raw_teso))
                                    
                                    # (Tarjeta de crédito: NO se envía a Google Sheets; a la hoja solo va lo que se cause como CXP)
                                    
                                    st.toast(f"✅ T.C. Causada exitosamente: {doc_num_siigo}", icon="🎉"); st.rerun()
                                else: st.error(msg)
                    with c_act2:
                        if st.button("🔄 A Pendientes", key=f"btn_ret_tc_{llave_factura}", use_container_width=True):
                            doc["Estado"], doc["Clasificacion"] = "Pendiente", ""
                            db_save_doc(curr_tenant_nit, r['ID'], "FC", "Pendiente", doc, r['NIT']); st.rerun()

# ----------------------------------------------------
# PANEL 4: DOCUMENTO SOPORTE (DS)
# ----------------------------------------------------
elif panel_seleccionado == "📄 4. Documentos Soporte (DS)":
    page_title("📄 Nuevo Documento Soporte Electrónico (DS)")
    if not can_cause: st.warning("🔒 No tienes permisos para causar en Siigo.")
    else:
        st.caption("Solo aparecen documentos aprobados para enviar a Siigo (No Caja Menor).")
        ds_aprobados = db_get_docs(curr_tenant_nit, "DS", "Aprobado")
        ds_regulares = [d for d in ds_aprobados if d.get("Clasificacion") != "Caja Menor"]
        ds_regulares = sorted(ds_regulares, key=lambda x: str(x.get("fecha", "")), reverse=True)
        
        if not ds_regulares: st.info("🎉 No hay Documentos Soporte aprobados pendientes por causar.")
        else:
            terceros_lista, cc_lista = maestros.get("terceros_lista", []), maestros.get("centros_costo", [])
            types_ds, pagos_lista, prods_lista = maestros.get("doc_types_ds", [{"id": 25872, "nombre": "DS - 1 - Doc. Soporte Exterior (ID: 25872)"}]), maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}]), maestros.get("productos", [])
            list_iva, list_rete, list_reteiva, list_ica = maestros.get("impuestos_iva", []), maestros.get("impuestos_rete", []), maestros.get("impuestos_reteiva", []), maestros.get("impuestos_ica", [])
            engine = PredictiveEngine(maestros)

            curr_m = ""
            for idx_doc, ds in enumerate(ds_regulares):
                curr_m = render_month_header(curr_m, ds.get("fecha", ""))
                
                llave_ds = f"ds_{ds['documento_ref']}_{idx_doc}"
                with st.container(border=True):
                    c_head1, c_head2 = st.columns([4, 1])
                    with c_head1: st.markdown(f"**Documento Soporte: {ds['documento_ref']} - {ds['proveedor']}**")
                    with c_head2:
                        if ds.get("pdf_b64"): st.download_button("💾 Ver PDF Original", data=safe_b64decode(ds["pdf_b64"]), file_name=f"DS_{ds['documento_ref']}.pdf", mime="application/pdf", key=f"dl_ds_pdf_caus_{llave_ds}")
                    
                    st.markdown("---")
                    f1, f2, f3, f4 = st.columns([2, 1.8, 1.8, 1.5])
                    with f1:
                        dt_sel = st.selectbox("Tipo DS Siigo", options=[t["nombre"] for t in types_ds], key=f"ds_dt_{llave_ds}")
                        id_type_ds = next((t["id"] for t in types_ds if t["nombre"] == dt_sel), 25872)
                        idx_terc_def = buscar_indice_tercero(ds['proveedor'], ds['nit'], terceros_lista)
                        opciones_terc = terceros_lista.copy()
                        if idx_terc_def < 0: opciones_terc.insert(0, f"⚠️ TERCERO NO CREADO EN SIIGO ({ds['nit']})"); tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=0, key=f"ds_terc_{llave_ds}")
                        else: tercero_sel = st.selectbox("Proveedores", options=opciones_terc, index=idx_terc_def, key=f"ds_terc_{llave_ds}")
                        nit_ingresado, prov_nombre = (ds['nit'], ds['proveedor']) if "⚠️" in tercero_sel else (tercero_sel.split(" - ")[0].strip(), tercero_sel.split(" - ", 1)[1].strip())
                    with f2:
                        fecha_ds = st.text_input("Fecha", value=ds['fecha'], key=f"ds_fec_{llave_ds}")
                        doc_ref = st.text_input("No. Comprobante", value=re.sub(r'\D', '', str(ds['documento_ref'])), key=f"ds_ref_{llave_ds}")
                    with f3:
                        cc_opts_ds = ["-- Sin Centro de Costo --"] + [c["nombre"] for c in cc_lista]
                        cc_header_sel = st.selectbox("Centro Costo", options=cc_opts_ds, index=cc_opts_ds.index(ds.get("centro_costo")) if ds.get("centro_costo") in cc_opts_ds else 0, key=f"ds_cc_head_{llave_ds}")
                        id_cc_head = next((c["id"] for c in cc_lista if c["nombre"] == cc_header_sel), None) if cc_header_sel != "-- Sin Centro de Costo --" else None
                        moneda_sel = st.selectbox("Moneda", options=["USD", "COP"], index=0 if ds['moneda_origen']=="USD" else 1, key=f"ds_mon_{llave_ds}")
                    with f4:
                        monto_orig = st.number_input("Monto Origen", value=float(ds['monto_origen']), key=f"ds_monto_{llave_ds}")
                        trm_val = st.number_input("TRM", value=float(ds['trm']), key=f"ds_trm_{llave_ds}") if moneda_sel=="USD" else 1.0
                        if moneda_sel == "USD": st.caption(f"💵 **Base COP (TRM):** ${round(monto_orig * trm_val, 2):,.2f}")

                    st.markdown("<div class='siigo-table-header'># | Tipo | Código / Producto | Descripción | Cant | Vr. Unitario | Imp. Cargo (IVA) | Imp. Retención | Valor Total | Acciones</div>", unsafe_allow_html=True)
                    if not ds.get("items_custom"): ds["items_custom"] = [{"type": "Account", "code": engine.predict_mapping(prov_nombre, nit_ingresado, "")["puc_code"], "description": f"Servicios Exterior - {prov_nombre}", "quantity": 1.0, "price": monto_orig, "id_iva": 0, "id_rete": 0}]

                    items_ds_siigo, acum_subtotal_ds, acum_iva_ds = [], 0.0, 0.0
                    for item_idx, item in enumerate(ds.get("items_custom", [])):
                        d0, d1, d2, d3, d4, d5, d6, d7, d8, d9 = st.columns([0.3, 0.8, 1.8, 1.7, 0.5, 1.0, 1.3, 1.7, 0.9, 0.4])
                        with d0: st.markdown(f"**{item_idx+1}**")
                        with d1: tipo_item = st.selectbox("Tipo", options=["Account", "Product"], index=0 if item.get("type", "Account")=="Account" else 1, key=f"ds_tp_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d2:
                            if tipo_item == "Account":
                                cat_puc = curr_tenant.get('puc', DEFAULT_PUC)
                                puc_sel = st.selectbox("Código PUC", options=cat_puc, index=0, key=f"ds_puc_{llave_ds}_{item_idx}", label_visibility="collapsed")
                                code_item = re.sub(r'[^\d]', '', puc_sel.split(" - ")[0].strip())
                            else:
                                prod_sel = st.selectbox("Producto Siigo", options=prods_lista if prods_lista else ["Sin Productos"], key=f"ds_prod_{llave_ds}_{item_idx}", label_visibility="collapsed")
                                code_item = prod_sel.split(" - ")[0].strip()
                        with d3: desc_val = st.text_input("Descripción", value=item.get('description', f"Servicio - {prov_nombre}"), key=f"ds_desc_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d4: cant_val = st.number_input("Cant", value=float(item.get('quantity', 1.0)), key=f"ds_cant_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d5: monto_val = st.number_input("Vr. Unitario", value=float(item.get('price', monto_orig)), key=f"ds_val_{llave_ds}_{item_idx}", label_visibility="collapsed")
                        with d6:
                            iva_sel = st.selectbox("Imp. Cargo", options=[i["nombre"] for i in list_iva], index=0, key=f"ds_iva_{llave_ds}_{item_idx}", label_visibility="collapsed")
                            id_iva, pct_iva_sel = next((i["id"] for i in list_iva if i["nombre"] == iva_sel), 0), next((i["porcentaje"] for i in list_iva if i["nombre"] == iva_sel), 0.0)
                        with d7:
                            rete_sel = st.selectbox("Imp. Retención", options=[i["nombre"] for i in list_rete], index=0, key=f"ds_rete_{llave_ds}_{item_idx}", label_visibility="collapsed")
                            id_rete = next((i["id"] for i in list_rete if i["nombre"] == rete_sel), 0)
                        with d8:
                            sub_row = round(monto_val * cant_val, 2); iva_row = round(sub_row * (pct_iva_sel / 100.0), 2); st.markdown(f"**{'$' if moneda_sel=='COP' else 'USD $'}{sub_row + iva_row:,.2f}**")
                        with d9:
                            if len(ds["items_custom"]) > 1 and st.button("🗑️", key=f"btn_del_line_ds_{llave_ds}_{item_idx}"): ds["items_custom"].pop(item_idx); db_save_doc(curr_tenant_nit, ds['documento_ref'], "DS", "Aprobado", ds, ds['nit']); st.rerun()

                        acum_subtotal_ds += sub_row; acum_iva_ds += iva_row
                        items_ds_siigo.append({"code": code_item, "type": tipo_item, "description": desc_val, "quantity": cant_val, "price": monto_val, "cost_center": id_cc_head, "id_iva": id_iva, "id_rete": id_rete, "pct_iva": pct_iva_sel})

                    if st.button("➕ Agregar Línea Adicional", key=f"btn_add_line_ds_{llave_ds}"): ds["items_custom"].append({"type": "Account", "code": "51355001", "description": "Línea Adicional Exterior", "quantity": 1.0, "price": 0.0, "id_iva": 0, "id_rete": 0}); db_save_doc(curr_tenant_nit, ds['documento_ref'], "DS", "Aprobado", ds, ds['nit']); st.rerun()

                    st.markdown("---")
                    b1, b2, b3 = st.columns([2, 1.8, 1.8])
                    with b1:
                        pago_sel = st.selectbox("Forma de pago", options=[p["nombre"] for p in maestros.get("pagos", [{"id": 1, "nombre": "Efectivo / Crédito (ID: 1)"}])], key=f"ds_pago_{llave_ds}")
                        id_pago = next((p["id"] for p in maestros.get("pagos", []) if p["nombre"] == pago_sel), 1)
                    with b2:
                        sel_reteiva_ds = st.selectbox("ReteIVA (Pie)", options=[i["nombre"] for i in list_reteiva], key=f"ds_glob_reteiva_{llave_ds}")
                        id_reteiva_ds = next((i["id"] for i in list_reteiva if i["nombre"] == sel_reteiva_ds), 0)
                    with b3:
                        sel_reteica_ds = st.selectbox("ReteICA (Pie)", options=[i["nombre"] for i in list_ica], key=f"ds_glob_reteica_{llave_ds}")
                        id_reteica_ds = next((i["id"] for i in list_ica if i["nombre"] == sel_reteica_ds), 0)

                    total_enviar_ds = acum_subtotal_ds + acum_iva_ds
                    st.markdown(f"### **Total Neto: {'$' if moneda_sel=='COP' else 'USD $'}{total_enviar_ds:,.2f} {moneda_sel}**")

                    c_act1, c_act2 = st.columns([3, 1])
                    with c_act1:
                        if st.button("🚀 Transmitir Documento Soporte a Siigo", key=f"btn_ds_send_{llave_ds}", type="primary", use_container_width=True):
                            if "⚠️" in tercero_sel: st.error("🔴 Selecciona un tercero válido.")
                            else:
                                num_ref_clean = re.sub(r'\D', '', doc_ref) or "101"
                                valid_items_ds = [it for it in items_ds_siigo if it["price"] > 0 or len(items_ds_siigo) == 1]
                                val_retenciones_calc_ds, ret_desglose_ds = 0.0, {}
                                
                                for it in valid_items_ds:
                                    sub_lin = it["quantity"] * it["price"]
                                    if it["id_rete"] and int(it["id_rete"]) > 0:
                                        pct_rete = next((float(i["porcentaje"]) for i in list_rete if i["id"] == int(it["id_rete"])), 0); nom_rete = next((i["nombre"] for i in list_rete if i["id"] == int(it["id_rete"])), f"Retencion {pct_rete}%"); val_r = sub_lin * (pct_rete/100.0); val_retenciones_calc_ds += val_r; ret_desglose_ds[nom_rete] = ret_desglose_ds.get(nom_rete, 0) + val_r

                                if id_reteiva_ds and int(id_reteiva_ds) > 0:
                                    pct_rete = next((float(i["porcentaje"]) for i in list_reteiva if i["id"] == int(id_reteiva_ds)), 0); val_r = acum_iva_ds * (pct_rete/100.0); val_retenciones_calc_ds += val_r; ret_desglose_ds[sel_reteiva_ds] = ret_desglose_ds.get(sel_reteiva_ds, 0) + val_r
                                    
                                if id_reteica_ds and int(id_reteica_ds) > 0:
                                    pct_rete = next((float(i["porcentaje"]) for i in list_ica if i["id"] == int(id_reteica_ds)), 0); val_r = acum_subtotal_ds * (pct_rete/100.0); val_retenciones_calc_ds += val_r; ret_desglose_ds[sel_reteica_ds] = ret_desglose_ds.get(sel_reteica_ds, 0) + val_r

                                for idx_item, it in enumerate(items_ds_siigo):
                                    if it["id_rete"] and int(it["id_rete"]) > 0: ds["items_custom"][idx_item]["Retencion_Nombre"] = next((i["nombre"] for i in list_rete if i["id"] == int(it["id_rete"])), "Rete")
                                    else: ds["items_custom"][idx_item]["Retencion_Nombre"] = "0%"

                                final_items_payload_ds = []
                                for it in valid_items_ds:
                                    taxes_list = []
                                    if it["id_iva"] and int(it["id_iva"]) > 0: taxes_list.append({"id": int(it["id_iva"])})
                                    if it["id_rete"] and int(it["id_rete"]) > 0: taxes_list.append({"id": int(it["id_rete"])})
                                    item_dict = {"code": it["code"], "type": it["type"], "description": it["description"], "quantity": it["quantity"], "price": it["price"], "taxes": taxes_list}
                                    if it["cost_center"]: item_dict["cost_center"] = it["cost_center"]
                                    final_items_payload_ds.append(item_dict)

                                retentions_payload_ds = []
                                if id_reteiva_ds and int(id_reteiva_ds) > 0: retentions_payload_ds.append({"id": int(id_reteiva_ds)})
                                if id_reteica_ds and int(id_reteica_ds) > 0: retentions_payload_ds.append({"id": int(id_reteica_ds)})

                                total_pagar_final_ds = total_enviar_ds - val_retenciones_calc_ds
                                payload_ds = {"document": {"id": id_type_ds}, "date": fecha_ds, "supplier": {"identification": nit_ingresado, "branch_office": 0}, "retentions": retentions_payload_ds, "observations": f"Documento Soporte (Ref: {num_ref_clean})", "items": final_items_payload_ds, "payments": [{"id": id_pago, "value": round(total_pagar_final_ds, 2), "due_date": fecha_ds}], "supplier_receipt_number": {"prefix": "DS", "number": int(num_ref_clean[-10:])}, "electronic_type": "Electronic", "is_electronic": True}
                                if id_cc_head: payload_ds["cost_center"] = id_cc_head
                                if moneda_sel == "USD": payload_ds["currency"] = {"code": "USD", "exchange_rate": float(trm_val)}

                                exito, msg, doc_id_siigo, doc_num_siigo, real_total_pagar = causar_en_siigo_api(payload_ds, True, curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'])
                                if exito:
                                    ds["subtotal"] = acum_subtotal_ds; ds["iva"] = acum_iva_ds; ds["retenciones"] = round(total_enviar_ds - real_total_pagar, 2); ds["Retenciones_Desglose"] = ret_desglose_ds; ds["FormaPago"] = pago_sel; ds["TotalPagar"] = real_total_pagar; ds["total"] = total_enviar_ds; ds["UsuarioCausador"] = curr_user["email"]; ds["FechaCausacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    db_save_history(curr_tenant_nit, ds['documento_ref'], "DS", fecha_ds, total_enviar_ds, moneda_sel, f"{doc_num_siigo}|||{doc_id_siigo}", prov_nombre, nit_ingresado, ds.get("pdf_b64"), json.dumps(ds), curr_user["email"])
                                    
                                    # 🔥 ENVIAMOS A TESORERÍA CON ESTRUCTURA EXACTA (DS)
                                    concepto_teso = " | ".join([str(i.get("description", "")) for i in ds.get("items_custom", [])]) or "Documento Soporte"
                                    cc_teso = ds.get("centro_costo", "-- Sin Centro de Costo --")
                                    clasif_t = ds.get("Clasificacion_Teso", "Proveedor")
                                    raw_teso = {
                                        "Empresa": curr_tenant['razon_social'],
                                        "Fecha Recibido": ds.get("fecha", datetime.now().strftime("%Y-%m-%d")),
                                        "Fecha Vto": ds.get("FechaVencimiento", ds.get("fecha", datetime.now().strftime("%Y-%m-%d"))),
                                        "Nit": nit_ingresado,
                                        "Proveedor": prov_nombre,
                                        "No. documento": str(num_ref_clean),
                                        "Concepto": concepto_teso,
                                        "Centro de Costo": cc_teso,
                                        "Valor con Iva": total_enviar_ds,
                                        "Clasificacion": clasif_t,
                                        "Estado": "Por pagar",
                                        "Valor a Pagar - Vr fra USD": real_total_pagar
                                    }
                                    raw_teso["_destino"] = ds.get("Clasificacion") or "CXP"
                                    db_save_treasury(curr_tenant_nit, str(num_ref_clean), prov_nombre, nit_ingresado, ds["fecha"], ds.get("FechaVencimiento", ds["fecha"]), concepto_teso, cc_teso, total_enviar_ds, real_total_pagar, "Por pagar", clasif_t, raw_data=json.dumps(raw_teso))
                                    
                                    # 🔔 WEBHOOK GOOGLE SHEETS (DS)
                                    _adj_ds = [] if not destino_es_cxp(ds.get("Clasificacion")) else armar_adjuntos_webhook({"id_doc_prov": ds["documento_ref"], "tipo": "DS", "fecha": fecha_ds, "total": total_enviar_ds, "moneda": moneda_sel, "id_siigo_num": f"{doc_num_siigo}|||{doc_id_siigo}", "proveedor": prov_nombre, "nit": nit_ingresado, "data_json": json.dumps(ds), "usuario": curr_user["email"]}, curr_tenant)
                                    enviar_fila_webhook(construir_fila_webhook(
                                        "DS", curr_tenant, curr_user["email"], "DS", num_ref_clean, doc_num_siigo,
                                        prov_nombre, nit_ingresado, fecha_ds, ds.get("FechaVencimiento", fecha_ds),
                                        moneda_sel, trm_val if moneda_sel == "USD" else 1.0, cc_teso, concepto_teso,
                                        acum_subtotal_ds, acum_iva_ds, round(total_enviar_ds - real_total_pagar, 2),
                                        total_enviar_ds, real_total_pagar, pago_sel, clasif_t, "Por pagar"), "DS", adjuntos=_adj_ds, destino=ds.get("Clasificacion") or "CXP")
                                    
                                    st.toast(f"✅ Causada exitosamente: {doc_num_siigo}", icon="🎉"); st.rerun()
                                else: st.error(msg)
                    with c_act2:
                        if st.button("🔄 A Pendientes", key=f"btn_ret_ds_{llave_ds}", use_container_width=True):
                            ds["estado"], ds["Clasificacion"] = "Pendiente", ""
                            db_save_doc(curr_tenant_nit, ds['documento_ref'], "DS", "Pendiente", ds, ds['nit']); st.rerun()

# ----------------------------------------------------
# PANEL 5: CAJA MENOR (SOLO REPORTE)
# ----------------------------------------------------
elif panel_seleccionado == "📦 5. Caja Menor":
    page_title("📦 Legalizaciones de Caja Menor")
    st.caption("Estos documentos NO se enviaron a Siigo. Están listos para exportarse en tu reporte interno.")
    
    docs_caja = db_get_docs(curr_tenant_nit, "FC", "Caja Menor") + db_get_docs(curr_tenant_nit, "DS", "Caja Menor")
    
    if not docs_caja: st.info("No tienes documentos clasificados como Caja Menor.")
    else:
        # Sort desc by date
        docs_caja = sorted(docs_caja, key=lambda x: str(x.get("Resumen", {}).get("Fecha", x.get("fecha", ""))), reverse=True)
        curr_m = ""
        for idx_cm, d in enumerate(docs_caja):
            tipo_txt = "FC" if "Resumen" in d else "DS"
            r = d["Resumen"] if tipo_txt == "FC" else d
            fecha = r["Fecha"] if tipo_txt == "FC" else r["fecha"]
            
            curr_m = render_month_header(curr_m, fecha)
            
            ref = r["ID"] if tipo_txt == "FC" else r["documento_ref"]
            prov = r["Proveedor"] if tipo_txt == "FC" else r["proveedor"]
            total = r["Total"] if tipo_txt == "FC" else r.get("monto_origen", 0)
            llave_caja = f"{tipo_txt}_{ref}_{r.get('NIT', r.get('nit'))}"
            
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([2, 4, 2, 2])
                with c1: st.markdown(f"### {tipo_txt}-{ref}\n**{fecha}**")
                with c2: st.markdown(f"#### {prov}\n<span class='badge-caja'>📦 Caja Menor</span>", unsafe_allow_html=True)
                with c3: st.metric("Total Gasto (COP)", f"${total:,.2f}")
                with c4:
                    if d.get("pdf_b64"): st.download_button("💾 Ver PDF", data=safe_b64decode(d["pdf_b64"]), file_name=f"CajaMenor_{ref}.pdf", mime="application/pdf", key=f"dl_caja_pdf_{llave_caja}", use_container_width=True)
                    if can_approve and st.button("🔄 A Pendientes", key=f"btn_return_caja_{llave_caja}", use_container_width=True):
                        if tipo_txt == "FC": d["Estado"], d["Clasificacion"] = "Pendiente", ""
                        else: d["estado"], d["Clasificacion"] = "Pendiente", ""
                        db_save_doc(curr_tenant_nit, ref, tipo_txt, "Pendiente", d, r.get("NIT", r.get("nit"))); st.rerun()

# ----------------------------------------------------
# PANEL 6: TABLERO DE AUDITORÍA (HISTÓRICO)
# ----------------------------------------------------
elif panel_seleccionado == "📊 6. Tablero Audit (Ajustes)":
    page_title("📊 Histórico de Causaciones (Siigo)")
    hist = db_get_history(curr_tenant_nit)
    if not hist: st.info("No hay documentos causados en la base de datos para esta empresa.")
    else:
        curr_m = ""
        for idx, c in enumerate(hist):
            curr_m = render_month_header(curr_m, c['fecha'])
            
            raw_siigo_str = c.get('id_siigo_num', '') or ''
            num_display = raw_siigo_str.split("|||", 1)[0] if "|||" in raw_siigo_str else raw_siigo_str
            with st.container(border=True):
                c1, c2, c3, c4, c5 = st.columns([1.5, 2.5, 1.5, 2, 2])
                with c1: st.markdown(f"⚡ <span class='badge-siigo'>{num_display}</span>", unsafe_allow_html=True); st.caption(f"Ref: {c['tipo']} #{c['id_doc_prov']} | {c['fecha']}")
                with c2: st.markdown(f"**{c['proveedor']}**"); st.caption(f"NIT: {c['nit']}")
                with c3: 
                    data_json = json.loads(c.get('data_json', '{}'))
                    total_pagar_actual = data_json.get('Resumen', {}).get('TotalPagar', c['total']) if c['tipo'] == 'FC' else data_json.get('TotalPagar', c['total'])
                    
                    if total_pagar_actual > (c['total'] * 1.5):
                        total_pagar_actual = c['total'] - float(data_json.get('Resumen', {}).get('Retenciones', 0.0) if c['tipo'] == 'FC' else data_json.get('retenciones', 0.0))
                    
                    st.markdown(f"**Total a Pagar:** {'$' if c['moneda'] == 'COP' else 'USD $'}{total_pagar_actual:,.2f}")
                with c4:
                    if c["pdf_original"]: st.download_button("📄 PDF Origen", data=c["pdf_original"], file_name=f"Original_{c['id_doc_prov']}.pdf", key=f"dl_o_{c['id_doc_prov']}_{c['tipo']}", use_container_width=True)
                    pdf_causacion = generar_comprobante_pdf(c, curr_tenant['razon_social'], curr_tenant['nit'])
                    st.download_button("⚡ PDF Contable", data=pdf_causacion, file_name=f"Causacion_{c['tipo']}_{c['id_doc_prov']}.pdf", mime="application/pdf", key=f"btn_dl_pdf_auto_{c['id_doc_prov']}_{c['tipo']}", use_container_width=True)
                with c5:
                    if c["tipo"] == "FC":
                        if can_cause and st.button("⚖️ Ajuste CC (ReteICA)", key=f"btn_adj_ica_{c['id_doc_prov']}_{c['tipo']}", use_container_width=True): modal_ajuste_ica(c, maestros, curr_tenant.get('puc', DEFAULT_PUC), curr_tenant_nit, curr_tenant['siigo_user'], curr_tenant['siigo_key'], curr_user['email'])

# ----------------------------------------------------
# PANEL 7: REPORTES Y EXPORTACIONES
# ----------------------------------------------------
elif panel_seleccionado == "📈 7. Reportes y Excel":
    page_title("📈 Reportes y Exportaciones a Excel")
    st.caption("Descarga informes detallados con desglose exacto de información para contabilidad.")

    tab_r1, tab_r2, tab_r3 = st.tabs(["⏳ Por Causar (Siigo)", "⚡ Causados en Siigo", "📦 Caja Menor"])
    
    with tab_r1:
        docs_aprobados = db_get_docs(curr_tenant_nit, "FC", "Aprobado") + db_get_docs(curr_tenant_nit, "DS", "Aprobado")
        docs_pendientes = [d for d in docs_aprobados if d.get("Clasificacion") != "Caja Menor"]
        if not docs_pendientes: st.info("No hay documentos pendientes para enviar a Siigo.")
        else:
            filas_aprob = []
            for d in docs_pendientes:
                v = extraer_valores_reporte(d)
                filas_aprob.append({"Fecha de Recibido": v["fecha_recibido"], "Fecha Vencimiento": v["fecha_vencimiento"], "NIT": v["nit"], "Proveedor": v["proveedor"], "Número de Factura": v["doc_ref"], "Concepto": v["conceptos"], "Centro de Costo": v["centro_costo"], "Valor con IVA": v["valor_con_iva"], "Retenciones": v["retenciones"], "Valor a Pagar": v["valor_a_pagar"], "Clasificación": v.get("clasificacion", "Proveedor")})
            df_aprob = pd.DataFrame(filas_aprob)
            df_aprob = df_aprob.sort_values(by="Fecha de Recibido", ascending=False)
            st.dataframe(df_aprob, use_container_width=True, height=250)
            st.download_button("📥 Descargar Excel (Por Causar)", data=generar_excel(df_aprob), file_name=f"Reporte_PorCausar_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab_r2:
        historial = db_get_history(curr_tenant_nit)
        if not historial: st.info("No hay documentos causados.")
        else:
            filas_caus = []
            for h in historial:
                data_j = json.loads(h.get("data_json") or "{}")
                v = extraer_valores_reporte(data_j, h_total=h["total"]) if data_j else {"fecha_recibido": h["fecha"], "fecha_vencimiento": h["fecha"], "nit": h["nit"], "proveedor": h["proveedor"], "doc_ref": h["id_doc_prov"], "conceptos": f"Causación {h['tipo']}", "centro_costo": "-- Sin Centro de Costo --", "valor_con_iva": h["total"], "retenciones": 0.0, "valor_a_pagar": h["total"], "clasificacion": "Proveedor"}
                filas_caus.append({"Fecha de Recibido": v["fecha_recibido"], "Fecha Vencimiento": v["fecha_vencimiento"], "NIT": v["nit"], "Proveedor": v["proveedor"], "Número de Factura": v["doc_ref"], "Concepto": v["conceptos"], "Centro de Costo": v["centro_costo"], "Valor con IVA": v["valor_con_iva"], "Retenciones": v["retenciones"], "Valor a Pagar": v["valor_a_pagar"], "Clasificación": v.get("clasificacion", "Proveedor")})
            df_caus = pd.DataFrame(filas_caus)
            df_caus = df_caus.sort_values(by="Fecha de Recibido", ascending=False)
            st.dataframe(df_caus, use_container_width=True, height=250)
            st.download_button("📥 Descargar Excel (Causados)", data=generar_excel(df_caus), file_name=f"Reporte_Causados_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab_r3:
        docs_caja = db_get_docs(curr_tenant_nit, "FC", "Caja Menor") + db_get_docs(curr_tenant_nit, "DS", "Caja Menor")
        if not docs_caja: st.info("No hay documentos en Caja Menor.")
        else:
            filas_caja = []
            for d in docs_caja:
                v = extraer_valores_reporte(d)
                filas_caja.append({"Fecha de Recibido": v["fecha_recibido"], "Fecha Vencimiento": v["fecha_vencimiento"], "NIT": v["nit"], "Proveedor": v["proveedor"], "Número de Factura": v["doc_ref"], "Concepto": v["conceptos"], "Centro de Costo": v["centro_costo"], "Valor con IVA": v["valor_con_iva"], "Retenciones": v["retenciones"], "Valor a Pagar": v["valor_a_pagar"], "Clasificación": v.get("clasificacion", "Proveedor")})
            df_caja = pd.DataFrame(filas_caja)
            df_caja = df_caja.sort_values(by="Fecha de Recibido", ascending=False)
            st.dataframe(df_caja, use_container_width=True, height=250)
            st.download_button("📥 Descargar Excel (Caja Menor)", data=generar_excel(df_caja), file_name=f"Reporte_CajaMenor_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ----------------------------------------------------
# PANEL 8: TESORERÍA (CXP & PAGOS)
# ----------------------------------------------------
elif panel_seleccionado == "💰 8. Tesorería (CXP & Pagos)":
    page_title("💰 Tesorería (Gestión de Cuentas por Pagar)")
    st.caption("Control de vencimientos, programación y ejecución de pagos a proveedores.")
    
    col_t_btn1, col_t_btn2 = st.columns([1.5, 3])
    with col_t_btn1:
        with st.expander("📥 Cargar Saldos Iniciales (Excel)"):
            st.caption("Sube el archivo de CXP. Asegúrate que tenga una hoja llamada 'Proveedores'.")
            if not can_admin: st.info("🔒 Solo los administradores pueden cargar saldos iniciales.")
            excel_cxp = st.file_uploader("Archivo de Saldos Iniciales", type=["xlsx", "xls"], label_visibility="collapsed", disabled=not can_admin)
            if excel_cxp:
                try:
                    df_cxp = pd.read_excel(excel_cxp, sheet_name="Proveedores")
                    
                    df_cxp = df_cxp[df_cxp['Empresa'].astype(str).str.contains('DAVINCI|DVT', case=False, na=False)]
                    
                    filas_cargadas = 0
                    errores = 0
                    for idx, r in df_cxp.iterrows():
                        try:
                            prov = str(r.get("Proveedor", "")).strip()
                            if not prov or prov.lower() == 'nan': continue
                            
                            doc_ref = str(r.get("No. documento", "")).strip()
                            if not doc_ref or doc_ref.lower() == 'nan': doc_ref = f"SD-{idx}"
                            
                            nit = str(r.get("Nit", "0")).strip()
                            
                            def parse_date(val):
                                if pd.isna(val): return ""
                                if isinstance(val, datetime) or pd.api.types.is_datetime64_any_dtype(val): return val.strftime("%Y-%m-%d")
                                return str(val)[:10]
                                
                            f_rec = parse_date(r.get("Fecha Recibido"))
                            f_venc = parse_date(r.get("Fecha Vto"))
                            
                            val_iva = safe_float(r.get("Valor con Iva", 0))
                            
                            raw_pagar = r.get("Valor a Pagar - Vr fra USD")
                            if pd.isna(raw_pagar) or str(raw_pagar).strip() == "": tot_pagar = val_iva
                            else: tot_pagar = safe_float(raw_pagar)
                            
                            if val_iva > 0 and tot_pagar > (val_iva * 2):
                                tot_pagar = val_iva
                            
                            concepto = str(r.get("Concepto", "Saldo Inicial"))
                            cc = str(r.get("Centro de Costo", "N/A"))
                            
                            est_raw = str(r.get("Estado", "Por pagar")).strip().lower()
                            if 'pagado' in est_raw or 'pago' in est_raw: estado = 'Pagado'
                            elif 'programado' in est_raw: estado = 'Programado'
                            else: estado = 'Por pagar'
                            
                            clasif = str(r.get("Clasificacion", "Proveedor"))
                            
                            raw_dict = {}
                            for k, v in r.items():
                                if pd.isna(v): raw_dict[str(k)] = ""
                                elif isinstance(v, (datetime, pd.Timestamp)): raw_dict[str(k)] = v.strftime("%Y-%m-%d")
                                else: raw_dict[str(k)] = str(v)
                            
                            nit_digitos = re.sub(r'\D', '', nit)
                            unique_id = f"{curr_tenant_nit}_{nit_digitos}_{doc_ref}_{idx}"
                            
                            raw_dict["_destino"] = "CXP"   # los saldos iniciales vienen de la hoja de CXP
                            db_save_treasury(curr_tenant_nit, doc_ref, prov, nit, f_rec, f_venc, concepto, cc, val_iva, tot_pagar, estado, clasif, raw_data=json.dumps(raw_dict), unique_id=unique_id)
                            filas_cargadas += 1
                        except Exception:
                            errores += 1
                            continue
                    
                    if errores > 0:
                        st.warning(f"⚠️ Se cargaron {filas_cargadas} registros de tu empresa, ignorando {errores} filas corruptas.")
                    else:
                        st.success(f"✅ Se cargaron {filas_cargadas} saldos iniciales (Filtrados por DVT/DAVINCI).")
                except Exception as e:
                    st.error(f"❌ Error al abrir el Excel. Asegúrate de que el archivo sea válido y tenga la hoja 'Proveedores'.")

    tesoreria_data = db_get_treasury(curr_tenant_nit)
    
    if not tesoreria_data:
        st.info("No hay registros en Tesorería. Causa una factura o carga el archivo de saldos iniciales.")
    else:
        hoy = datetime.now()
        data_analisis = []
        for t in tesoreria_data:
            try:
                f_venc = datetime.strptime(t["fecha_vencimiento"][:10], "%Y-%m-%d")
                dias_mora = (hoy - f_venc).days
            except: dias_mora = -1
            
            t["dias_mora"] = dias_mora
            t["categoria"] = calcular_categoria_dias(dias_mora) if t["estado"] != "Pagado" else "Pagado"
            
            raw_parsed = json.loads(t["raw_data"]) if t["raw_data"] else {}
            t["origen"] = "AutoCount (Nuevos)" if not raw_parsed else "Saldos Iniciales"
            
            data_analisis.append(t)
            
        df_teso = pd.DataFrame(data_analisis)
        df_teso["mes_clave"] = df_teso["fecha_recibido"].apply(obtener_mes_str)
        df_teso = df_teso.sort_values(by="fecha_recibido", ascending=False)
        
        df_por_pagar = df_teso[df_teso["estado"] != "Pagado"]
        
        # DASHBOARD DE TESORERÍA (MÉTRICAS MAESTRAS)
        st.markdown("#### 📊 Análisis Financiero Total")
        t_iva = df_teso["valor_con_iva"].sum()
        t_pagar = df_teso["total_pagar"].sum()
        t_pagado = df_teso[df_teso["estado"] == "Pagado"]["total_pagar"].sum()
        t_deuda = df_por_pagar["total_pagar"].sum()
        
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Total Histórico a Pagar (Menos Retenciones)", f"${t_pagar:,.2f}")
        col_m2.metric("Total Gestionado y Pagado", f"${t_pagado:,.2f}")
        col_m3.metric("Saldo Real por Pagar (Deuda Activa)", f"${t_deuda:,.2f}")
        
        st.markdown("<br><b>⏳ Desglose de Deuda Activa (Por Vencimiento)</b>", unsafe_allow_html=True)
        m1, m2, m3, m4, m5 = st.columns(5)
        vigente = df_por_pagar[df_por_pagar["categoria"] == "Vigente"]["total_pagar"].sum()
        m1_30 = df_por_pagar[df_por_pagar["categoria"] == "1 a 30"]["total_pagar"].sum()
        m31_60 = df_por_pagar[df_por_pagar["categoria"] == "31 a 60"]["total_pagar"].sum()
        m61_90 = df_por_pagar[df_por_pagar["categoria"] == "61 a 90"]["total_pagar"].sum()
        mayor_90 = df_por_pagar[df_por_pagar["categoria"] == "Mayor a 90"]["total_pagar"].sum()
        
        m1.metric("✅ Al Día (Vigente)", f"${vigente:,.0f}")
        m2.metric("⚠️ 1 a 30 Días", f"${m1_30:,.0f}")
        m3.metric("🟧 31 a 60 Días", f"${m31_60:,.0f}")
        m4.metric("🔴 61 a 90 Días", f"${m61_90:,.0f}")
        m5.metric("❌ Mayor a 90 Días", f"${mayor_90:,.0f}")
        
        st.markdown("---")
        
        # Filtros Maestros
        col_f1, col_f2, col_f3, col_f4 = st.columns(4)
        f_est = col_f1.selectbox("Filtrar por Estado:", ["Todos", "Por pagar", "Programado", "Pagado"], index=0)
        
        df_teso["prov_base"] = df_teso["proveedor"].apply(lambda x: str(x).split("-")[0].strip() if pd.notnull(x) else "")
        prov_opts = ["Todos"] + sorted(list(df_teso[df_teso["prov_base"] != ""]["prov_base"].unique()))
        f_prov = col_f2.selectbox("Filtrar por Proveedor:", prov_opts)
        
        f_origen = col_f3.selectbox("Filtrar por Origen:", ["Todos", "AutoCount (Nuevos)", "Saldos Iniciales"], index=0)
        
        # Filtro de Mes
        meses_opts = ["Todos"] + list(df_teso["mes_clave"].unique())
        f_mes = col_f4.selectbox("Filtrar por Mes:", meses_opts)
        
        # Filtrar datos
        df_filtrado = df_teso.copy()
        if f_est != "Todos": df_filtrado = df_filtrado[df_filtrado["estado"] == f_est]
        if f_prov != "Todos": df_filtrado = df_filtrado[df_filtrado["prov_base"] == f_prov]
        if f_origen != "Todos": df_filtrado = df_filtrado[df_filtrado["origen"] == f_origen]
        if f_mes != "Todos": df_filtrado = df_filtrado[df_filtrado["mes_clave"] == f_mes]
        
        st.markdown("#### 💳 Listado de Cuentas (Gestión en Línea)")
        
        # Paginación Ultra Rápida
        filas_por_pagina = 15
        if 'teso_page' not in st.session_state: st.session_state['teso_page'] = 1
        
        total_paginas = math.ceil(len(df_filtrado) / filas_por_pagina)
        if total_paginas == 0: total_paginas = 1
        if st.session_state['teso_page'] > total_paginas: st.session_state['teso_page'] = total_paginas
        
        inicio = (st.session_state['teso_page'] - 1) * filas_por_pagina
        fin = inicio + filas_por_pagina
        df_pagina = df_filtrado.iloc[inicio:fin]
        
        c_pag1, c_pag2, c_pag3 = st.columns([1, 2, 1])
        with c_pag1:
            if st.button("⬅️ Anterior") and st.session_state['teso_page'] > 1:
                st.session_state['teso_page'] -= 1
                st.rerun()
        with c_pag2: st.markdown(f"<div style='text-align:center;'>Página <b>{st.session_state['teso_page']}</b> de {total_paginas} (Total registros: {len(df_filtrado)})</div>", unsafe_allow_html=True)
        with c_pag3:
            if st.button("Siguiente ➡️") and st.session_state['teso_page'] < total_paginas:
                st.session_state['teso_page'] += 1
                st.rerun()

        # Falso Encabezado de Tabla
        st.markdown("""
        <div style="display:flex; justify-content:space-between; align-items:center; background-color: #f8fafc; padding: 10px; border-radius: 6px; font-weight: 800; font-size: 0.75rem; color: #475569; margin-bottom: 5px; border: 1px solid #e2e8f0; text-transform: uppercase;">
            <div style="width: 15%;">Factura/Ref</div>
            <div style="width: 25%;">Proveedor</div>
            <div style="width: 15%;">Vencimiento</div>
            <div style="width: 15%;">Valor Original</div>
            <div style="width: 15%;">Valor a Pagar</div>
            <div style="width: 10%;">Estado</div>
            <div style="width: 5%;">Gestión</div>
        </div>
        """, unsafe_allow_html=True)

        curr_m_teso = ""
        for idx_t, row in df_pagina.iterrows():
            
            # Etiqueta de mes
            if f_mes == "Todos":
                curr_m_teso = render_month_header(curr_m_teso, row['fecha_recibido'])
            
            color_borde = "border-left: 4px solid #3b82f6;" if row["estado"] == "Programado" else ("border-left: 4px solid #22c55e;" if row["estado"] == "Pagado" else "border-left: 4px solid #f59e0b;")
            
            with st.container():
                st.markdown(f"""
                <div style="display:flex; justify-content:space-between; align-items:center; font-size: 0.8rem; margin-bottom: 5px; padding-left: 8px; {color_borde}">
                    <div style="width: 15%; font-weight:bold;">{row['doc_ref']}</div>
                    <div style="width: 25%; font-weight:bold; color: #0f172a;">{row['proveedor'][:35]}</div>
                    <div style="width: 15%; color: #64748b;">{row['fecha_vencimiento'][:10]}</div>
                    <div style="width: 15%;">${row['valor_con_iva']:,.2f}</div>
                    <div style="width: 15%; font-weight:bold;">${row['total_pagar']:,.2f}</div>
                    <div style="width: 10%;"><span style="background-color: #f1f5f9; padding: 3px 6px; border-radius: 4px;">{row['estado']}</span></div>
                    <div style="width: 5%;">↓</div>
                </div>
                """, unsafe_allow_html=True)
                
                with st.expander(f"⚙️ Gestionar Pagos - Doc: {row['doc_ref']}"):
                    if not can_treasury: st.caption("🔒 Solo lectura: tu rol no puede modificar pagos.")
                    with st.form(f"form_teso_{row['id']}"):
                        c_num1, c_num2 = st.columns(2)
                        edit_val_iva = c_num1.number_input("Valor Original con IVA", value=float(row["valor_con_iva"]), step=1000.0)
                        edit_val_pagar = c_num2.number_input("Valor a Pagar Real", value=float(row["total_pagar"]), step=1000.0)

                        c_form1, c_form2, c_form3, c_form4 = st.columns(4)
                        nuevo_estado = c_form1.selectbox("Estado del Pago", ["Por pagar", "Programado", "Pagado"], index=["Por pagar", "Programado", "Pagado"].index(row["estado"]))
                        fecha_prop = c_form2.date_input("Fecha Programada Pago", value=datetime.strptime(row["fecha_propuesta"][:10], "%Y-%m-%d") if row["fecha_propuesta"] else datetime.now())
                        fecha_real = c_form3.date_input("Fecha Real de Pago", value=datetime.strptime(row["fecha_pago"][:10], "%Y-%m-%d") if row["fecha_pago"] else datetime.now())
                        banco_girador = c_form4.selectbox("Banco Girador", ["", "Bancolombia", "Davivienda", "Banco de Bogotá", "Tarjeta de Crédito"], index=0 if not row["banco_girador"] else ["", "Bancolombia", "Davivienda", "Banco de Bogotá", "Tarjeta de Crédito"].index(row["banco_girador"]))
                        obs_pago = st.text_area("Observaciones", value=row["observacion"] if row["observacion"] else "")
                        
                        if st.form_submit_button("💾 Guardar Actualización", type="primary", disabled=not can_treasury):
                            db_save_treasury(
                                curr_tenant_nit, row["doc_ref"], row["proveedor"], row["nit_proveedor"], 
                                row["fecha_recibido"], row["fecha_vencimiento"], row["concepto"], row["centro_costo"], 
                                float(edit_val_iva), float(edit_val_pagar), nuevo_estado, row["clasificacion"], 
                                fecha_prop.strftime("%Y-%m-%d"), obs_pago, fecha_real.strftime("%Y-%m-%d") if nuevo_estado == "Pagado" else "", banco_girador, row.get("raw_data", "{}"), row["id"]
                            )
                            
                            # 🔔 WEBHOOK GOOGLE SHEETS (TESORERÍA)
                            enviar_fila_webhook(construir_fila_webhook(
                                "Tesorería", curr_tenant, curr_user["email"], "TESO", row["doc_ref"], "",
                                row["proveedor"], row["nit_proveedor"], row["fecha_recibido"], row["fecha_vencimiento"],
                                "COP", 1.0, row["centro_costo"], row["concepto"], 0.0, 0.0,
                                float(edit_val_iva) - float(edit_val_pagar), float(edit_val_iva), float(edit_val_pagar),
                                banco_girador, row["clasificacion"], nuevo_estado,
                                fecha_pago=fecha_real.strftime("%Y-%m-%d") if nuevo_estado == "Pagado" else "",
                                obs=obs_pago,
                                fecha_prop=fecha_prop.strftime("%Y-%m-%d")), "Tesorería", destino="CXP" if fila_tesoreria_va_a_sheets(row.get("raw_data")) else "Tarjeta")
                            
                            st.toast("✅ Documento actualizado exitosamente.", icon="💰")
                            st.rerun()

        st.markdown("---")
        st.markdown("#### 📥 Exportar Reporte de Tesorería")
        st.caption("Solo se descargará lo que esté visible según los filtros de arriba.")
        
        filas_export = []
        for idx, t in df_filtrado.iterrows():
            try: r_data = json.loads(t.get("raw_data") or "{}")
            except: r_data = {}
            r_data = {k: v for k, v in r_data.items() if not str(k).startswith("_")}   # sin marcas internas
            
            r_data["Empresa"] = curr_tenant['razon_social']
            r_data["Estado"] = t["estado"]
            r_data["Fecha propuesta Pago"] = t["fecha_propuesta"]
            r_data["Fecha de Pago"] = t["fecha_pago"]
            r_data["Banco Girador"] = t["banco_girador"]
            r_data["Observaciòn"] = t["observacion"]
            
            r_data["Valor con Iva"] = t["valor_con_iva"]
            r_data["Valor a Pagar - Vr fra USD"] = t["total_pagar"]
            r_data["Categoria"] = t["categoria"]
            
            if "Proveedor" not in r_data: r_data["Proveedor"] = t["proveedor"]
            if "No. documento" not in r_data: r_data["No. documento"] = t["doc_ref"]
            if "Fecha Recibido" not in r_data: r_data["Fecha Recibido"] = t["fecha_recibido"]
            if "Fecha Vto" not in r_data: r_data["Fecha Vto"] = t["fecha_vencimiento"]
            if "Nit" not in r_data: r_data["Nit"] = t["nit_proveedor"]
            if "Concepto" not in r_data: r_data["Concepto"] = t["concepto"]
            if "Centro de Costo" not in r_data: r_data["Centro de Costo"] = t["centro_costo"]
            if "Clasificacion" not in r_data: r_data["Clasificacion"] = t["clasificacion"]
            
            filas_export.append(r_data)
            
        df_export = pd.DataFrame(filas_export)
        
        if not df_export.empty:
            st.download_button("📊 Descargar Excel Exportable", data=generar_excel(df_export), file_name=f"Reporte_Tesoreria_{curr_tenant_nit}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

# ----------------------------------------------------
# PANEL 9: CONFIGURACIÓN MULTI-EMPRESA & USUARIOS
# ----------------------------------------------------
elif panel_seleccionado == "⚙️ Configuración Empresa":
    if not can_admin: st.warning("🔒 Acceso denegado. Solo administradores.")
    else:
        page_title("⚙️ Configuración de Empresa y Gestión Multi-Empresa")
        if curr_rol == "SuperAdmin":
            with st.expander("🏢 Crear Nueva Empresa (Tenant SaaS)", expanded=False):
                with st.form("form_create_tenant"):
                    new_t_nit = st.text_input("NIT de la Empresa")
                    new_t_razon = st.text_input("Razón Social")
                    new_t_user = st.text_input("Correo Usuario API Siigo")
                    new_t_key = st.text_input("Access Key API Siigo", type="password")
                    if st.form_submit_button("🚀 Crear Empresa en AutoCount"):
                        if new_t_nit and new_t_razon:
                            conn = get_db_connection()
                            try:
                                conn.execute("INSERT INTO tenants VALUES (?,?,?,?,?)", (new_t_nit.strip(), new_t_razon.strip(), new_t_user.strip(), new_t_key.strip(), json.dumps(DEFAULT_PUC)))
                                conn.execute("INSERT INTO users (email, password, nombre, tenant_nit, rol, activo) VALUES (?,?,?,?,?,?)", (f"admin@{new_t_nit}.com", hash_password("123456", f"admin@{new_t_nit}.com"), f"Admin {new_t_razon}", new_t_nit.strip(), "Administrador", 1))
                                conn.commit(); st.success(f"✅ Empresa {new_t_razon} creada."); st.rerun()
                            except Exception as e: st.error(f"❌ Error: {e}")
                            finally: conn.close()
                        else: st.error("⚠️ Ingrese el NIT y la Razón Social.")

        st.markdown("---")
        if can_config:
            with st.form("form_tenant"):
                st.markdown(f"#### Ficha de Empresa Activa: **{curr_tenant['razon_social']}**")
                t_nit = st.text_input("NIT", value=curr_tenant['nit'], disabled=True)
                t_raz = st.text_input("Razón Social", value=curr_tenant['razon_social'])
                t_usr = st.text_input("Siigo User", value=curr_tenant['siigo_user'])
                t_key = st.text_input("Siigo Key", value=curr_tenant['siigo_key'], type="password")
                if st.form_submit_button("Guardar Cambios Empresa"):
                    conn = get_db_connection()
                    conn.execute("UPDATE tenants SET razon_social=?, siigo_user=?, siigo_key=? WHERE nit=?", (t_raz, t_usr, t_key, t_nit))
                    conn.commit(); conn.close(); st.success("Configuración actualizada."); st.rerun()

        if can_config:
            with st.expander("💾 Respaldo y restauración de datos", expanded=False):
                st.caption("Descarga una copia completa de los datos (incluye usuarios y llaves de Siigo: guárdala en un lugar seguro). También puedes importar el archivo autocount.db de tu instalación anterior.")
                if st.button("📦 Preparar respaldo", key="btn_prep_respaldo"):
                    st.session_state["respaldo_bytes"] = exportar_respaldo_sqlite()
                    st.session_state["respaldo_nombre"] = f"respaldo_autocount_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
                if st.session_state.get("respaldo_bytes"):
                    st.download_button("⬇️ Descargar respaldo", data=st.session_state["respaldo_bytes"], file_name=st.session_state.get("respaldo_nombre", "respaldo_autocount.db"), mime="application/octet-stream", key="dl_respaldo")
                st.markdown("---")
                archivo_import = st.file_uploader("Importar un archivo .db (tu autocount.db anterior o un respaldo)", type=["db"], key="up_import_db")
                confirma_import = st.checkbox("Entiendo que los registros con el mismo identificador se actualizarán con los del archivo (no se borra nada).", key="chk_import_db")
                if archivo_import is not None and confirma_import and st.button("⬆️ Importar ahora", type="primary", key="btn_import_db"):
                    try:
                        res_imp = importar_respaldo_sqlite(archivo_import.getvalue())
                        st.cache_data.clear()
                        st.success("✅ Importación terminada: " + ", ".join(f"{t}: {n}" for t, n in res_imp.items()) + ". Cierra sesión y vuelve a entrar.")
                    except Exception as e_imp: st.error(f"❌ No se pudo importar: {e_imp}")

        st.markdown("---")
        roles_ok = roles_asignables(curr_rol)
        col_u1, col_u2 = st.columns([1.5, 2])
        with col_u1:
            st.markdown("#### 👥 Registrar Usuario")
            with st.form("form_user"):
                u_email, u_pass, u_name = st.text_input("Correo"), st.text_input("Contraseña", type="password"), st.text_input("Nombre Completo")
                u_rol = st.selectbox("Rol Asignado", roles_ok)
                if st.form_submit_button("➕ Crear Usuario"):
                    if u_email and u_pass:
                        conn = get_db_connection()
                        try:
                            conn.execute("INSERT INTO users (email, password, nombre, tenant_nit, rol, activo) VALUES (?,?,?,?,?,?)", (u_email.lower().strip(), hash_password(u_pass, u_email), u_name, curr_tenant_nit, u_rol, 1))
                            conn.commit(); st.success(f"✅ Usuario {u_name} registrado."); st.rerun()
                        except Exception: st.error("❌ El correo ya está registrado.")
                        finally: conn.close()
                    else: st.error("Complete el correo y la contraseña.")

        usuarios_emp = db_listar_usuarios(curr_tenant_nit)
        with col_u2:
            st.markdown("#### 📋 Usuarios de esta empresa")
            if usuarios_emp: st.dataframe(pd.DataFrame([{"Correo": u["email"], "Nombre": u["nombre"], "Rol": u["rol"], "Estado": "✅ Activo" if u["activo"] else "⛔ Inactivo"} for u in usuarios_emp]), use_container_width=True, hide_index=True)
            else: st.info("No hay usuarios registrados.")

        st.markdown("---")
        st.markdown("#### ✏️ Modificar o eliminar usuarios")
        gestionables = [u for u in usuarios_emp if puede_gestionar_usuario(curr_rol, u["rol"])]
        if not gestionables: st.info("No hay usuarios que puedas gestionar.")
        else:
            mapa_u = {u["email"]: u for u in gestionables}
            u_sel = mapa_u.get(st.selectbox("Usuario (correo)", list(mapa_u.keys()))) or gestionables[0]   # solo el correo: así la selección no salta a otro usuario al guardar
            st.caption(f"{u_sel['nombre'] or '—'}  ·  {u_sel['rol']}  ·  {'✅ Activo' if u_sel['activo'] else '⛔ Inactivo'}")
            k_u = re.sub(r'\W', '_', u_sel["email"])
            with st.form(f"form_edit_user_{k_u}"):
                e_nombre = st.text_input("Nombre completo", value=u_sel["nombre"], key=f"e_nombre_{k_u}")
                e_rol = st.selectbox("Rol", roles_ok, index=roles_ok.index(u_sel["rol"]) if u_sel["rol"] in roles_ok else 0, key=f"e_rol_{k_u}")
                e_activo = st.checkbox("Usuario activo (puede iniciar sesión)", value=u_sel["activo"], key=f"e_activo_{k_u}")
                e_pass = st.text_input("Nueva contraseña (opcional: déjala vacía para no cambiarla)", type="password", key=f"e_pass_{k_u}")
                if st.form_submit_button("💾 Guardar cambios", type="primary"):
                    try:
                        db_actualizar_usuario(curr_user, u_sel["email"], curr_tenant_nit, e_nombre, e_rol, e_activo)
                        if e_pass: db_restablecer_password(curr_user, u_sel["email"], curr_tenant_nit, e_pass)
                        st.toast(f"✅ Cambios guardados para {u_sel['email']}.", icon="👤"); st.rerun()
                    except ValueError as err_u: st.error(f"❌ {err_u}")
            c_tmp, c_del = st.columns(2)
            with c_tmp.expander("🔑 Generar contraseña temporal"):
                st.caption("Crea una contraseña al azar, la aplica y la muestra para que se la entregues al usuario.")
                if st.button("Generar y aplicar", key=f"btn_tmp_{k_u}"):
                    _tmp = generar_password_temporal()
                    try:
                        db_restablecer_password(curr_user, u_sel["email"], curr_tenant_nit, _tmp)
                        st.session_state["pass_temporal"] = (u_sel["email"], _tmp)
                    except ValueError as err_u: st.error(f"❌ {err_u}")
                _pt = st.session_state.get("pass_temporal")
                if _pt and _pt[0] == u_sel["email"]:
                    st.success(f"Contraseña temporal de {_pt[0]}:")
                    st.code(_pt[1])
                    if st.button("Ocultar", key=f"btn_hide_tmp_{k_u}"): st.session_state.pop("pass_temporal", None); st.rerun()
            with c_del.expander("🗑️ Eliminar usuario"):
                st.warning("Esto borra el acceso de forma definitiva. Si solo quieres que no entre más, desmarca «Usuario activo» arriba: así conservas su historial y puedes reactivarlo.")
                conf_del = st.checkbox("Sí, quiero eliminar este usuario", key=f"conf_del_{k_u}")
                if st.button("Eliminar definitivamente", key=f"btn_del_user_{k_u}", disabled=not conf_del):
                    try:
                        db_eliminar_usuario(curr_user, u_sel["email"], curr_tenant_nit)
                        st.toast(f"🗑️ Usuario {u_sel['email']} eliminado.", icon="✅"); st.rerun()
                    except ValueError as err_u: st.error(f"❌ {err_u}")
