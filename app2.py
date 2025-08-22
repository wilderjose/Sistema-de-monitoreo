from flask import Flask, jsonify, render_template_string, session, redirect, request, Response
import serial
import time
import socket
import threading
import re
import os
import csv
import io
import json
from collections import deque
from datetime import datetime
from fpdf import FPDF

app = Flask(__name__)
app.secret_key = 'supersecretkey123'

# ============= CREDENCIALES DE USUARIO =============
USUARIOS = {
    "Wilder": "admin123",
    "operador": "operador456"
}

# ============= CONFIGURACIÓN ARDUINO =============
SERIAL_PORT = 'COM4'
BAUD_RATE = 9600

# ============= ESTADOS DEL SISTEMA =============
led_state = "off"
arduino = None
connection_status = "Conectando..."
puertos_disponibles = []
flash_message = {"text": "", "type": ""}  # Para mensajes temporales

sensor_data = {
    "IR": "Esperando datos...",
    "ULTRA": "Esperando datos...",
    "TEMP": "Esperando datos...",
    "BOMBA": "Esperando datos..."
}

# ============= CONTADORES Y REPORTES =============
llenados_vasos = 0
eventos_llenado = deque(maxlen=100)

# ============= DETECCIÓN DE PUERTOS DISPONIBLES =============
def detectar_puertos():
    global puertos_disponibles
    puertos_disponibles = []
    
    if os.name == 'nt':
        for i in range(1, 20):
            port_name = f'COM{i}'
            try:
                s = serial.Serial(port_name)
                s.close()
                puertos_disponibles.append(port_name)
            except (OSError, serial.SerialException):
                pass
    else:
        for port in ['/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyUSB0', '/dev/ttyUSB1']:
            if os.path.exists(port):
                puertos_disponibles.append(port)
    
    return puertos_disponibles

# ============= FUNCIONES DE CONEXIÓN =============
def conectar_arduino():
    global arduino, connection_status, SERIAL_PORT
    
    detectar_puertos()
    print(f"🔍 Puertos disponibles: {puertos_disponibles}")
    
    while True:
        try:
            if arduino is None or not arduino.is_open:
                print(f"🔌 Intentando conectar a {SERIAL_PORT}...")
                arduino = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
                time.sleep(2)
                arduino.flushInput()
                connection_status = "Conectado"
                print(f"[✅] Conexión establecida con Arduino en {SERIAL_PORT}")
        except serial.SerialException as e:
            if "PermissionError" in str(e) or "Acceso denegado" in str(e):
                connection_status = f"Error: Acceso denegado a {SERIAL_PORT}"
                print(f"[❌] Acceso denegado al puerto {SERIAL_PORT}. ¿Está abierto en otro programa?")
                
                if puertos_disponibles:
                    nuevo_puerto = next((p for p in puertos_disponibles if p != SERIAL_PORT), None)
                    if nuevo_puerto:
                        print(f"🔄 Intentando con puerto alternativo: {nuevo_puerto}")
                        SERIAL_PORT = nuevo_puerto
            else:
                connection_status = f"Error: {str(e)}"
                print(f"[❌] Error de conexión: {str(e)}")
            
            if arduino and arduino.is_open:
                try:
                    arduino.close()
                except:
                    pass
            arduino = None
        except Exception as e:
            connection_status = f"Error: {str(e)}"
            print(f"[❌] Error inesperado: {str(e)}")
            arduino = None
        
        time.sleep(3)

# ============= LECTURA DE DATOS SERIAL (CORREGIDA PARA TEMPERATURA) =============
def leer_serial():
    global sensor_data, led_state, llenados_vasos
    
    bomba_anterior = "Apagada"
    ir_anterior = "Sin vaso"
    
    while True:
        if arduino and arduino.is_open:
            try:
                if arduino.in_waiting > 0:
                    linea = arduino.readline().decode('utf-8', errors='ignore').strip()
                    if linea:
                        # Manejo de comandos simples
                        if "BANDA:ON" in linea:
                            led_state = "on"
                        elif "BANDA:OFF" in linea:
                            led_state = "off"
                        
                        # Intentar parsear como JSON
                        try:
                            if linea.startswith('{') and linea.endswith('}'):
                                data = json.loads(linea)
                                
                                # Extraer valores del JSON
                                ir_val = data.get("IR", 1)
                                ultra_val = data.get("ULTRA", 0)
                                temp_val = data.get("TEMP", 0)
                                bomba_str = data.get("BOMBA", "OFF")
                                
                                # Actualizar estado IR
                                nuevo_estado_ir = "Vaso detectado" if ir_val == 0 else "Sin vaso"
                                if ir_anterior == "Sin vaso" and nuevo_estado_ir == "Vaso detectado":
                                    eventos_llenado.append({
                                        'tipo': "Vaso",
                                        'estado': "Colocado",
                                        'timestamp': time.strftime("%Y-%m-%d %H:%M:%S"),
                                        'nivel': f"{ultra_val} cm",
                                        'temp': f"{temp_val} °C"
                                    })
                                ir_anterior = nuevo_estado_ir
                                sensor_data["IR"] = nuevo_estado_ir
                                
                                # Actualizar ULTRA
                                sensor_data["ULTRA"] = f"{ultra_val} cm"
                                
                                # Actualizar TEMP - CORRECCIÓN IMPORTANTE
                                # Convertir a float y formatear correctamente
                                try:
                                    temp_val = float(temp_val)
                                    sensor_data["TEMP"] = f"{temp_val:.1f} °C"  # Un decimal
                                except (ValueError, TypeError):
                                    sensor_data["TEMP"] = "Error"
                                
                                # Actualizar BOMBA
                                nuevo_estado_bomba = "Encendida" if bomba_str == "ON" else "Apagada"
                                if bomba_anterior == "Encendida" and nuevo_estado_bomba == "Apagada":
                                    llenados_vasos += 1
                                    eventos_llenado.append({
                                        'tipo': "Vaso",
                                        'estado': "Llenado completado",
                                        'timestamp': time.strftime("%Y-%m-%d %H:%M:%S"),
                                        'nivel': f"{ultra_val} cm",
                                        'temp': f"{temp_val} °C"
                                    })
                                bomba_anterior = nuevo_estado_bomba
                                sensor_data["BOMBA"] = nuevo_estado_bomba
                                
                        except json.JSONDecodeError:
                            # Si falla el JSON, intentar con el formato antiguo
                            if re.match(r'IR:\d+,ULTRA:\d+\.?\d*,TEMP:\d+\.?\d*,BOMBA:[01]', linea):
                                partes = linea.split(',')
                                for parte in partes:
                                    if ':' in parte:
                                        key, val = parte.split(':')
                                        key = key.strip()
                                        
                                        if key == "IR":
                                            nuevo_estado = "Vaso detectado" if val == "0" else "Sin vaso"
                                            if ir_anterior == "Sin vaso" and nuevo_estado == "Vaso detectado":
                                                eventos_llenado.append({
                                                    'tipo': "Vaso",
                                                    'estado': "Colocado",
                                                    'timestamp': time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    'nivel': sensor_data["ULTRA"],
                                                    'temp': sensor_data["TEMP"]
                                                })
                                            ir_anterior = nuevo_estado
                                            sensor_data["IR"] = nuevo_estado
                                        
                                        elif key == "ULTRA":
                                            sensor_data["ULTRA"] = f"{val} cm"
                                        
                                        elif key == "TEMP":
                                            # Manejo correcto de temperatura
                                            try:
                                                temp_val = float(val)
                                                sensor_data["TEMP"] = f"{temp_val:.1f} °C"
                                            except (ValueError, TypeError):
                                                sensor_data["TEMP"] = "Error"
                                        
                                        elif key == "BOMBA":
                                            nuevo_estado = "Encendida" if val == "1" else "Apagada"
                                            if bomba_anterior == "Encendida" and nuevo_estado == "Apagada":
                                                llenados_vasos += 1
                                                eventos_llenado.append({
                                                    'tipo': "Vaso",
                                                    'estado': "Llenado completado",
                                                    'timestamp': time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    'nivel': sensor_data["ULTRA"],
                                                    'temp': sensor_data["TEMP"]
                                                })
                                            bomba_anterior = nuevo_estado
                                            sensor_data["BOMBA"] = nuevo_estado
                                
            except Exception as e:
                print(f"[ERROR] Lectura serial: {str(e)}")
                try:
                    arduino.flushInput()
                except:
                    pass
        time.sleep(0.1)
# ============= INTERFAZ WEB =============
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sistema de Banda Transportadora</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #4361ee;
            --secondary: #3f37c9;
            --success: #4cc9f0;
            --danger: #f72585;
            --warning: #ff9e00;
            --dark: #212529;
            --light: #f8f9fa;
            --gray: #6c757d;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, #0f1b3a, #1a2a6c, #2d388a);
            color: var(--light);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        
        .container {
            margin-top: 40px;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            gap: 25px;
            flex-wrap: wrap;
            width: 100%;
            max-width: 1400px;
        }

        .left-panel {
            flex: 0 0 35%;
            min-width: 340px;
            max-width: 500px;
        }

        .right-panel {
            flex: 0 0 60%;
            min-width: 340px;
            max-width: 800px;
        }

        @media (max-width: 1024px) {
            .left-panel,
            .right-panel {
                flex: 1 1 100%;
                max-width: 100%;
            }
        }
        
        .card {
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 25px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
            width: 100%;
            margin-bottom: 25px;
        }
        
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.3);
        }
        
        header {
            text-align: center;
            margin-bottom: 30px;
            width: 100%;
        }
        
        h1 {
            font-size: 2.8rem;
            margin-bottom: 10px;
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }
        
        .subtitle {
            color: #a3b2e8;
            font-size: 1.2rem;
            max-width: 600px;
            margin: 0 auto;
        }
        
        .card-title {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 1.8rem;
            margin-bottom: 25px;
            color: var(--success);
        }
        
        .card-title i {
            font-size: 2rem;
        }
        
        .control-panel {
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        
        .led-container {
            position: relative;
            width: 220px;
            height: 220px;
            margin: 20px auto;
        }
        
        .led-visual {
            width: 100%;
            height: 100%;
            border-radius: 50%;
            background: radial-gradient(circle at 30% 30%, #555, #000);
            box-shadow: inset 0 0 30px rgba(0, 0, 0, 0.8),
                        0 0 20px rgba(0, 0, 0, 0.3);
            position: relative;
            overflow: hidden;
            transition: all 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        }
        
        .led-visual.on {
            background: radial-gradient(circle at 30% 30%, #4cc9f0, #4361ee);
            box-shadow: inset 0 0 30px rgba(76, 201, 240, 0.6),
                        0 0 50px rgba(67, 97, 238, 0.8),
                        0 0 100px rgba(67, 97, 238, 0.4);
        }
        
        .led-visual.off {
            background: radial-gradient(circle at 30% 30%, #555, #000);
        }
        
        .led-status {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 1.5rem;
            font-weight: bold;
            color: white;
            text-shadow: 0 0 10px rgba(0, 0, 0, 0.5);
        }
        
        .controls {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            justify-content: center;
            width: 100%;
            margin: 20px 0;
        }
        
        .btn {
            padding: 16px 35px;
            font-size: 1.2rem;
            border: none;
            border-radius: 50px;
            cursor: pointer;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 600;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
        }
        
        .btn:active {
            transform: translateY(3px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        }
        
        .btn-on {
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            color: white;
        }
        
        .btn-off {
            background: linear-gradient(to right, #f72585, #b5179e);
            color: white;
        }

        .btn-export {
            background: linear-gradient(to right, #00c853, #009624);
            color: white;
        }

        .btn-pdf {
            background: linear-gradient(to right, #e53935, #e35d5b);
            color: white;
        }
        
        .btn i {
            font-size: 1.4rem;
        }
        
        .status-message {
            margin-top: 25px;
            padding: 15px;
            border-radius: 15px;
            background: rgba(255, 255, 255, 0.1);
            text-align: center;
            font-size: 1.1rem;
            min-height: 60px;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            transition: all 0.3s ease;
        }
        
        .status-success {
            background: rgba(76, 201, 240, 0.2);
            border: 1px solid var(--success);
            color: #4cc9f0;
        }
        
        .status-error {
            background: rgba(247, 37, 133, 0.2);
            border: 1px solid var(--danger);
            color: #f72585;
        }
        
        .sensors-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            width: 100%;
        }
        
        .sensor-card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 15px;
            padding: 25px;
            display: flex;
            flex-direction: column;
            align-items: center;
            transition: transform 0.3s ease;
            position: relative;
            overflow: hidden;
            height: 100%;
            min-height: 240px;
        }
        
        .sensor-card:hover {
            transform: translateY(-5px);
            background: rgba(255, 255, 255, 0.08);
        }
        
        .sensor-status {
            position: absolute;
            top: 10px;
            right: 10px;
            width: 15px;
            height: 15px;
            border-radius: 50%;
            background: var(--danger);
        }
        
        .sensor-status.active {
            background: var(--success);
            box-shadow: 0 0 10px var(--success);
        }
        
        .sensor-icon {
            font-size: 3.5rem;
            margin-bottom: 20px;
            color: #4cc9f0;
        }
        
        .sensor-name {
            font-size: 1.4rem;
            font-weight: 600;
            margin-bottom: 5px;
            color: #a3b2e8;
        }
        
        .sensor-value {
            font-size: 2rem;
            font-weight: 700;
            margin: 10px 0;
            text-align: center;
            min-height: 60px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        
        .sensor-unit {
            font-size: 1.1rem;
            color: var(--gray);
        }
        
        .sensor-ir .sensor-value {
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800;
        }
        
        .sensor-ultra .sensor-value {
            color: #ff9e00;
        }
        
        .sensor-temp .sensor-value {
            color: #f72585;
        }
        
        .sensor-bomba .sensor-value {
            color: #4cc9f0;
        }
        
        .connection-status {
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 10px 20px;
            border-radius: 50px;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 0, 0, 0.4);
            backdrop-filter: blur(5px);
            z-index: 100;
        }
        
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
        }
        
        .status-connected {
            background-color: #4cc9f0;
            box-shadow: 0 0 10px #4cc9f0;
        }
        
        .status-disconnected {
            background-color: #f72585;
            box-shadow: 0 0 10px #f72585;
        }
        
        footer {
            margin-top: 40px;
            text-align: center;
            color: rgba(255, 255, 255, 0.6);
            font-size: 0.9rem;
            width: 100%;
            padding: 20px;
        }
        
        .system-status {
            display: flex;
            justify-content: center;
            gap: 15px;
            margin-top: 20px;
            flex-wrap: wrap;
        }
        
        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 15px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 50px;
            font-size: 0.9rem;
        }
        
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }
        
        .status-ok {
            background: var(--success);
            box-shadow: 0 0 5px var(--success);
        }
        
        .status-warning {
            background: var(--warning);
            box-shadow: 0 0 5px var(--warning);
        }
        
        .status-error {
            background: var(--danger);
            box-shadow: 0 0 5px var(--danger);
        }
        
        .user-info {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 8px 16px;
            border-radius: 50px;
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(5px);
            display: flex;
            align-items: center;
            gap: 10px;
            z-index: 100;
        }
        
        .logout-btn {
            background: none;
            border: none;
            color: #f72585;
            cursor: pointer;
            font-size: 1.2rem;
            transition: all 0.3s ease;
        }
        
        .logout-btn:hover {
            transform: scale(1.1);
        }

        /* ============= ESTILOS PARA REPORTES ============= */
        .report-section {
            margin-top: 30px;
            width: 100%;
        }
        
        .report-card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 15px;
            padding: 20px;
            margin-top: 15px;
        }
        
        .counters {
            display: flex;
            justify-content: space-around;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 15px;
        }
        
        .counter-item {
            text-align: center;
            padding: 15px;
            background: rgba(76, 201, 240, 0.1);
            border-radius: 15px;
            min-width: 200px;
        }
        
        .counter-value {
            font-size: 2.5rem;
            font-weight: bold;
            margin: 10px 0;
            color: #4cc9f0;
        }
        
        .counter-label {
            font-size: 1.2rem;
            color: #a3b2e8;
        }
        
        .event-log {
            max-height: 300px;
            overflow-y: auto;
            padding: 10px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 10px;
        }
        
        .event-item {
            padding: 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .event-item:last-child {
            border-bottom: none;
        }
        
        .event-type {
            font-weight: bold;
            width: 100px;
            padding: 5px 10px;
            border-radius: 20px;
            text-align: center;
            font-size: 0.9rem;
        }
        
        .event-type.vaso {
            background: rgba(76, 201, 240, 0.2);
            color: #4cc9f0;
        }
        
        .event-status {
            flex: 2;
            text-align: center;
            padding: 0 10px;
        }
        
        .event-status.completed {
            color: #4cc9f0;
        }
        
        .event-status.placed {
            color: #ff9e00;
        }
        
        .event-details {
            flex: 3;
            text-align: right;
            font-size: 0.9rem;
            color: #a3b2e8;
        }

        .export-section {
            margin-top: 20px;
            display: flex;
            justify-content: center;
            gap: 15px;
            flex-wrap: wrap;
        }

        /* ============= MENSAJES FLASH ============= */
        .flash-container {
            position: fixed;
            top: 80px;
            right: 20px;
            z-index: 1000;
            max-width: 400px;
        }
        
        .flash-message {
            padding: 15px 20px;
            border-radius: 10px;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 12px;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.2);
            animation: slideIn 0.3s ease, fadeOut 0.5s ease 4.5s forwards;
            transform: translateX(120%);
            opacity: 0;
        }
        
        .flash-success {
            background: linear-gradient(135deg, #4cc9f0, #4361ee);
            color: white;
        }
        
        .flash-error {
            background: linear-gradient(135deg, #f72585, #b5179e);
            color: white;
        }
        
        .flash-icon {
            font-size: 1.5rem;
        }
        
        .flash-content {
            flex: 1;
        }
        
        .flash-close {
            background: none;
            border: none;
            color: white;
            cursor: pointer;
            font-size: 1.2rem;
        }
        
        @keyframes slideIn {
            from { transform: translateX(120%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        
        @keyframes fadeOut {
            from { transform: translateX(0); opacity: 1; }
            to { transform: translateX(120%); opacity: 0; }
        }

        /* ============= MENSAJE DE INICIO DE SESIÓN ============= */
        .login-confirmation {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            padding: 12px 25px;
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            color: white;
            border-radius: 50px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.3);
            z-index: 1000;
            opacity: 0;
            transition: opacity 0.5s, transform 0.5s;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .login-confirmation.show {
            opacity: 1;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(76, 201, 240, 0.7); }
            70% { box-shadow: 0 0 0 10px rgba(76, 201, 240, 0); }
            100% { box-shadow: 0 0 0 0 rgba(76, 201, 240, 0); }
        }

        @media (min-width: 768px) {
            .sensors-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }
    </style>
</head>
<body>
    <!-- Contenedor para mensajes flash -->
    <div class="flash-container" id="flash-container"></div>
    
    <!-- Mensaje de inicio de sesión -->
    <div id="login-confirmation" class="login-confirmation">
        <i class="fas fa-check-circle"></i>
        <span>¡Sesión iniciada correctamente!</span>
    </div>
    
    <div class="user-info">
        <i class="fas fa-user-circle"></i>
        <span>{{ session.usuario }}</span>
        <a href="/logout" class="logout-btn" title="Cerrar sesión">
            <i class="fas fa-sign-out-alt"></i>
        </a>
    </div>
    
    <header>
        <h1><i class="fas fa-industry"></i> Control de Banda Transportadora</h1>
        <p class="subtitle">Sistema automatizado para llenado de vasos</p>
    </header>
    
    <div class="system-status">
        <div class="status-item">
            <span class="status-dot" id="arduino-status-dot"></span>
            <span>Arduino: <span id="arduino-status-text">Conectando...</span></span>
        </div>
        <div class="status-item">
            <span class="status-dot status-ok"></span>
            <span>Sensores: <span id="sensor-status">Activos</span></span>
        </div>
        <div class="status-item">
            <span class="status-dot status-ok"></span>
            <span>Sistema: <span id="system-status">Operativo</span></span>
        </div>
    </div>
    
    <div class="container">
        <div class="left-panel">
            <!-- SECCIÓN DE CONTROL DE BANDA -->
            <div class="card control-panel">
                <h2 class="card-title"><i class="fas fa-sliders-h"></i> Control de Banda</h2>
                
                <div class="led-container">
                    <div id="led-visual" class="led-visual off">
                        <div class="led-status">APAGADA</div>
                    </div>
                </div>
                
                <div class="controls">
                    <button class="btn btn-on" onclick="controlLED('on')">
                        <i class="fas fa-play-circle"></i> INICIAR BANDA
                    </button>
                    <button class="btn btn-off" onclick="controlLED('off')">
                        <i class="fas fa-stop-circle"></i> DETENER BANDA
                    </button>
                </div>
                
                <div id="message" class="status-message" style="margin-top: 40px;">
                    Sistema listo para operar. Presione INICIAR para comenzar.
                </div>
            </div>
        </div>
        
        <div class="right-panel">
            <!-- SECCIÓN DE MONITOR DE SENSORES -->
            <div class="card">
                <h2 class="card-title"><i class="fas fa-sensor"></i> Monitoreo de Sensores</h2>
                
                <div class="sensors-grid">
                    <div class="sensor-card sensor-ir">
                        <div class="sensor-status" id="ir-status"></div>
                        <i class="sensor-icon fas fa-glass-water"></i>
                        <div class="sensor-name">Sensor de Vaso (IR)</div>
                        <div class="sensor-value" id="ir">Esperando datos...</div>
                        <div class="sensor-unit">Estado de detección</div>
                    </div>
                    
                    <div class="sensor-card sensor-ultra">
                        <div class="sensor-status" id="ultra-status"></div>
                        <i class="sensor-icon fas fa-ruler-vertical"></i>
                        <div class="sensor-name">Nivel de Llenado</div>
                        <div class="sensor-value" id="ultra">0 cm</div>
                        <div class="sensor-unit">Distancia ultrasónica</div>
                    </div>
                    
                    <div class="sensor-card sensor-temp">
                        <div class="sensor-status" id="temp-status"></div>
                        <i class="sensor-icon fas fa-thermometer-half"></i>
                        <div class="sensor-name">Temperatura</div>
                        <div class="sensor-value" id="temp">0 °C</div>
                        <div class="sensor-unit">Temperatura ambiente</div>
                    </div>
                    
                    <div class="sensor-card sensor-bomba">
                        <div class="sensor-status" id="bomba-status"></div>
                        <i class="sensor-icon fas fa-faucet-drip"></i>
                        <div class="sensor-name">Estado de Bomba</div>
                        <div class="sensor-value" id="bomba">Apagada</div>
                        <div class="sensor-unit">Bomba de llenado</div>
                    </div>
                </div>
            </div>
            
            <!-- SECCIÓN DE REPORTES -->
            <div class="card" style="margin-top: 30px;">
                <h2 class="card-title"><i class="fas fa-chart-bar"></i> Reporte de Llenados</h2>
                
                <div class="counters">
                    <div class="counter-item">
                        <div class="counter-label">Vasos llenados</div>
                        <div class="counter-value" id="vasos-count">0</div>
                    </div>
                </div>
                
                <div class="report-card">
                    <h3><i class="fas fa-history"></i> Historial de eventos</h3>
                    <div class="event-log" id="event-log">
                        <div class="event-item">
                            <div class="event-details">Esperando eventos...</div>
                        </div>
                    </div>
                </div>

                <div class="export-section">
                    <button class="btn btn-export" onclick="exportarCSV()">
                        <i class="fas fa-file-csv"></i> Exportar a CSV
                    </button>
                    <button class="btn btn-pdf" onclick="exportarPDF()">
                        <i class="fas fa-file-pdf"></i> Exportar a PDF
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <div class="connection-status">
        <span class="status-indicator" id="connection-indicator"></span>
        <span>Servidor: <span id="ip-address">localhost:5000</span></span>
    </div>
    
    <footer>
        <p>Sistema de Automatización Industrial | Control de Banda Transportadora</p>
        <p>Tiempo Real &copy; 2023</p>
    </footer>

    <script>
        // Función para controlar el estado de la banda
        function controlLED(state) {
            fetch('/led/' + state)
            .then(res => res.json())
            .then(data => {
                const led = document.getElementById("led-visual");
                const msg = document.getElementById("message");
                
                // Actualizar LED visual
                led.classList.remove("on", "off");
                
                if (data.state === "on") {
                    led.classList.add("on");
                    led.querySelector('.led-status').textContent = "ENCENDIDA";
                } else {
                    led.classList.add("off");
                    led.querySelector('.led-status').textContent = "APAGADA";
                }
                
                // Actualizar mensaje de estado
                msg.textContent = data.message;
                msg.className = "status-message ";
                
                if (data.message.includes("✅")) {
                    msg.classList.add("status-success");
                } else if (data.message.includes("❌")) {
                    msg.classList.add("status-error");
                }
            });
        }
        
        // Función para actualizar los datos de los sensores
        function actualizarSensores() {
            fetch('/datos')
            .then(res => res.json())
            .then(data => {
                document.getElementById('ir').textContent = data.IR;
                document.getElementById('ultra').textContent = data.ULTRA;
                document.getElementById('temp').textContent = data.TEMP;
                document.getElementById('bomba').textContent = data.BOMBA;
                
                // Actualizar indicadores de estado
                const irStatus = document.getElementById('ir-status');
                irStatus.className = 'sensor-status ' + (data.IR.includes("detectado") ? 'active' : '');
                
                const ultraStatus = document.getElementById('ultra-status');
                try {
                    const ultraValue = parseFloat(data.ULTRA);
                    ultraStatus.className = 'sensor-status ' + (ultraValue < 10 ? 'active' : '');
                } catch {
                    ultraStatus.className = 'sensor-status';
                }
                
                const bombaStatus = document.getElementById('bomba-status');
                bombaStatus.className = 'sensor-status ' + (data.BOMBA.includes("Encendida") ? 'active' : '');
            });
        }
        
        // Función para actualizar el estado de conexión
        function actualizarEstadoConexion() {
            fetch('/estado-conexion')
            .then(res => res.json())
            .then(data => {
                const statusDot = document.getElementById('arduino-status-dot');
                const statusText = document.getElementById('arduino-status-text');
                const connectionIndicator = document.getElementById('connection-indicator');
                
                statusText.textContent = data.status;
                
                if (data.status.includes('Conectado')) {
                    statusDot.className = 'status-dot status-ok';
                    connectionIndicator.className = 'status-indicator status-connected';
                } else if (data.status.includes('Error')) {
                    statusDot.className = 'status-dot status-error';
                    connectionIndicator.className = 'status-indicator status-disconnected';
                } else {
                    statusDot.className = 'status-dot status-warning';
                    connectionIndicator.className = 'status-indicator status-disconnected';
                }
            });
        }
        
        // Función para actualizar el reporte de llenados
        function actualizarReporte() {
            fetch('/reporte-llenados')
            .then(res => res.json())
            .then(data => {
                document.getElementById('vasos-count').textContent = data.vasos;
                
                const eventLog = document.getElementById('event-log');
                eventLog.innerHTML = '';
                
                if (data.eventos.length === 0) {
                    eventLog.innerHTML = '<div class="event-item"><div class="event-details">No hay eventos registrados</div></div>';
                    return;
                }
                
                data.eventos.forEach(evento => {
                    const eventItem = document.createElement('div');
                    eventItem.className = 'event-item';
                    
                    const statusClass = evento.estado.includes("completado") ? "completed" : "placed";
                    
                    eventItem.innerHTML = `
                        <div class="event-type vaso">${evento.tipo}</div>
                        <div class="event-status ${statusClass}">${evento.estado}</div>
                        <div class="event-details">
                            ${evento.timestamp}<br>
                            ${evento.nivel} | ${evento.temp}
                        </div>
                    `;
                    
                    eventLog.prepend(eventItem);
                });
            });
        }

        // Función para exportar los datos a CSV
        function exportarCSV() {
            // Crear un enlace temporal para la descarga
            const enlace = document.createElement('a');
            enlace.href = '/exportar-csv';
            enlace.download = `reporte_llenados_${new Date().toISOString().slice(0, 10)}.csv`;
            document.body.appendChild(enlace);
            enlace.click();
            document.body.removeChild(enlace);
        }
        
        // Función para exportar los datos a PDF
        function exportarPDF() {
            // Crear un enlace temporal para la descarga
            const enlace = document.createElement('a');
            enlace.href = '/exportar-pdf';
            enlace.download = `reporte_llenados_${new Date().toISOString().slice(0, 10)}.pdf`;
            document.body.appendChild(enlace);
            enlace.click();
            document.body.removeChild(enlace);
        }
        
        // Función para obtener la IP local
        function getLocalIP() {
            return new Promise((resolve) => {
                resolve(window.location.hostname || 'localhost');
            });
        }
        
        // Función para mostrar mensajes flash
        function showFlashMessage(message, type) {
            const container = document.getElementById('flash-container');
            const messageEl = document.createElement('div');
            messageEl.className = `flash-message flash-${type}`;
            
            // Ícono según tipo de mensaje
            const icon = type === 'success' ? 'fa-check-circle' : 'fa-exclamation-circle';
            
            messageEl.innerHTML = `
                <i class="fas ${icon} flash-icon"></i>
                <div class="flash-content">${message}</div>
                <button class="flash-close" onclick="this.parentElement.remove()">
                    <i class="fas fa-times"></i>
                </button>
            `;
            
            container.appendChild(messageEl);
            
            // Eliminar automáticamente después de 5 segundos
            setTimeout(() => {
                if (messageEl.parentNode) {
                    messageEl.style.animation = 'fadeOut 0.5s ease forwards';
                    setTimeout(() => messageEl.remove(), 500);
                }
            }, 5000);
        }
        
        // Al cargar la página
        window.onload = async () => {
            // Obtener y mostrar IP local
            try {
                const ip = await getLocalIP();
                document.getElementById('ip-address').textContent = ip + ":5000";
            } catch {
                document.getElementById('ip-address').textContent = "localhost:5000";
            }
            
            // Inicializar estado
            controlLED('estado');
            
            // Actualizar sensores cada 500ms
            setInterval(actualizarSensores, 500);
            
            // Actualizar estado de conexión cada 2 segundos
            setInterval(actualizarEstadoConexion, 2000);
            actualizarEstadoConexion();
            
            // Actualizar reporte cada 2 segundos
            setInterval(actualizarReporte, 2000);
            actualizarReporte();
            
            // Comprobar si hay mensaje flash
            const response = await fetch('/get-flash-message');
            const flashData = await response.json();
            
            if (flashData.message) {
                showFlashMessage(flashData.message, flashData.type);
            }
            
            // Mostrar mensaje de inicio de sesión
            const loginConfirmation = document.getElementById('login-confirmation');
            if (loginConfirmation) {
                loginConfirmation.classList.add('show');
                setTimeout(() => {
                    loginConfirmation.classList.remove('show');
                }, 5000);
            }
        }
    </script>
</body>
</html>
"""

# ============= PLANTILLA DE LOGIN =============
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Acceso al Sistema</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #4361ee;
            --secondary: #3f37c9;
            --dark: #0f1b3a;
            --light: #f8f9fa;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, var(--dark), #1a2a6c);
            color: var(--light);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        
        .login-container {
            width: 100%;
            max-width: 450px;
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .logo {
            text-align: center;
            margin-bottom: 30px;
        }
        
        .logo i {
            font-size: 4rem;
            color: #4cc9f0;
            margin-bottom: 15px;
        }
        
        .logo h1 {
            font-size: 2.2rem;
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: #a3b2e8;
        }
        
        .input-with-icon {
            position: relative;
        }
        
        .input-with-icon i {
            position: absolute;
            left: 15px;
            top: 50%;
            transform: translateY(-50%);
            color: #4cc9f0;
        }
        
        .input-with-icon input {
            width: 100%;
            padding: 15px 15px 15px 50px;
            border: none;
            border-radius: 50px;
            background: rgba(255, 255, 255, 0.1);
            color: white;
            font-size: 1.1rem;
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: all 0.3s ease;
        }
        
        .input-with-icon input:focus {
            outline: none;
            border-color: #4361ee;
            background: rgba(67, 97, 238, 0.1);
        }
        
        .btn-login {
            width: 100%;
            padding: 16px;
            font-size: 1.2rem;
            border: none;
            border-radius: 50px;
            cursor: pointer;
            transition: all 0.3s ease;
            background: linear-gradient(to right, #4cc9f0, #4361ee);
            color: white;
            font-weight: 600;
            margin-top: 10px;
        }
        
        .btn-login:hover {
            transform: translateY(-3px);
            box-shadow: 0 5px 15px rgba(67, 97, 238, 0.4);
        }
        
        .error-message {
            background: rgba(247, 37, 133, 0.2);
            border: 1px solid #f72585;
            color: #f72585;
            padding: 15px;
            border-radius: 15px;
            margin-bottom: 25px;
            text-align: center;
        }
        
        .footer {
            text-align: center;
            margin-top: 30px;
            color: rgba(255, 255, 255, 0.6);
            font-size: 0.9rem;
        }
        
        /* ============= MENSAJES FLASH ============= */
        #flash-message-container {
                position: fixed;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                background-color: rgba(76, 201, 240, 0.95);
                color: #fff;
                padding: 40px 30px; /* Reduced padding for better mobile */
                border-radius: 20px;
                box-shadow: 0 8px 30px rgba(0,0,0,0.5);
                text-align: center;
                font-size: clamp(1.2rem, 4vw, 1.6rem); /* Responsive font size */
                z-index: 10000;
                max-width: min(800px, 90vw); /* Prevents overflow on large screens */
                min-width: min(400px, 85vw); /* Responsive minimum width */
                box-sizing: border-box; /* Includes padding in width calculation */
                display: none;
                min-height: 150px; /* Reduced minimum height */
                align-items: center;
                justify-content: center;
                flex-direction: column;
            }

            /* Different message types */
            .flash-success { background-color: rgba(76, 201, 240, 0.95); }
            .flash-error   { background-color: rgba(247, 37, 133, 0.95); }
            .flash-warning { background-color: rgba(255, 158, 0, 0.95); }

            /* Mobile optimization */
            @media (max-width: 480px) {
                #flash-message-container {
                    padding: 30px 20px;
                    min-height: 120px;
                    border-radius: 16px;
                }
            }
    </style>
    <div id="flash-message-container"></div>

</head>
<body>
    <!-- Contenedor para mensajes flash -->
    <div class="flash-container" id="flash-container"></div>
    
    <div class="login-container">
        <div class="logo">
            <i class="fas fa-industry"></i>
            <h1>Control Industrial</h1>
        </div>
        
        {% if error %}
        <div class="error-message">
            <i class="fas fa-exclamation-circle"></i> {{ error }}
        </div>
        {% endif %}
        
        <form method="POST">
            <div class="form-group">
                <label for="usuario">Usuario</label>
                <div class="input-with-icon">
                    <i class="fas fa-user"></i>
                    <input type="text" id="usuario" name="usuario" placeholder="Ingrese su usuario" required>
                </div>
            </div>
            
            <div class="form-group">
                <label for="password">Contraseña</label>
                <div class="input-with-icon">
                    <i class="fas fa-lock"></i>
                    <input type="password" id="password" name="password" placeholder="Ingrese su contraseña" required>
                </div>
            </div>
            
            <button type="submit" class="btn-login">
                <i class="fas fa-sign-in-alt"></i> INGRESAR AL SISTEMA
            </button>
        </form>
        
        <div class="footer">
            Sistema de Control Industrial &copy; 2023
        </div>
    </div>

    <div id="flash-message-container"></div>

        <script>
            function showFlashMessage(message, type="success", duration=3000) {
                const container = document.getElementById("flash-message-container");
                container.textContent = message;

                // Aplicar clase según tipo
                container.className = "";
                container.classList.add(`flash-${type}`);

                // Mostrar el mensaje
                container.style.display = "block";

                // Ocultar después de 'duration' ms
                setTimeout(() => {
                    container.style.display = "none";
                }, duration);
            }

            window.onload = () => {
                const urlParams = new URLSearchParams(window.location.search);
                const flashType = urlParams.get('flash_type');
                const flashMessage = urlParams.get('flash_message');
                
                if (flashType && flashMessage) {
                    showFlashMessage(decodeURIComponent(flashMessage), flashType);
                    
                    // Limpiar parámetros de la URL
                    const newUrl = window.location.origin + window.location.pathname;
                    window.history.replaceState({}, document.title, newUrl);
                }
            }
            </script>
</body>
</html>

"""

# ============= RUTAS FLASK =============
@app.route("/login", methods=['GET', 'POST'])
def login():
    global flash_message
    
    if request.method == 'POST':
        usuario = request.form.get('usuario')
        password = request.form.get('password')
        
        if usuario in USUARIOS and USUARIOS[usuario] == password:
            session['usuario'] = usuario
            session['autenticado'] = True
            flash_message = {
                "text": f"✅ ¡Bienvenido, {usuario}! Sesión iniciada correctamente.",
                "type": "success"
            }
            return redirect('/')
        else:
            return render_template_string(LOGIN_TEMPLATE, error="Credenciales inválidas")
    
    # Verificar si hay mensaje flash para mostrar (después de logout)
    flash_type = request.args.get('flash_type', '')
    flash_msg = request.args.get('flash_message', '')
    
    return render_template_string(LOGIN_TEMPLATE, error=None)

@app.route("/logout")
def logout():
    global flash_message
    
    usuario = session.get('usuario', 'Usuario')
    session.pop('autenticado', None)
    session.pop('usuario', None)
    
    flash_message = {
        "text": f"✅ {usuario}! Sesión cerrada correctamente.",
        "type": "success"
    }
    
    # Redirigir al login con mensaje flash
    return redirect(f'/login?flash_type=success&flash_message={flash_message["text"]}')

@app.route("/get-flash-message")
def get_flash_message():
    global flash_message
    message = flash_message
    flash_message = {"text": "", "type": ""}  # Limpiar después de leer
    return jsonify(message)

@app.route("/")
def index():
    if not session.get('autenticado'):
        return redirect('/login')
    return render_template_string(HTML_TEMPLATE, usuario=session.get('usuario', ''))

@app.route("/led/<state>")
def led_control(state):
    if not session.get('autenticado'):
        return jsonify({"error": "No autenticado"}), 401
    
    global led_state
    message = ""

    if state == "estado":
        return jsonify({"state": led_state, "message": "Estado actual consultado."})

    if arduino and arduino.is_open:
        try:
            command = '1\n' if state.lower() == "on" else '0\n'
            
            for _ in range(3):
                arduino.write(command.encode())
                time.sleep(0.1)
            
            led_state = state
            message = f"✅ Banda {state.upper()} - Comando ejecutado"
        except Exception as e:
            message = f"❌ Error: {str(e)}"
    else:
        message = "❌ Arduino no disponible"

    return jsonify({"state": state, "message": message})

@app.route("/datos")
def datos():
    if not session.get('autenticado'):
        return jsonify({"error": "No autenticado"}), 401
    return jsonify(sensor_data)

@app.route("/estado-conexion")
def estado_conexion():
    if not session.get('autenticado'):
        return jsonify({"error": "No autenticado"}), 401
    global connection_status
    return jsonify({"status": connection_status, "puerto": SERIAL_PORT, "disponibles": puertos_disponibles})

@app.route("/reporte-llenados")
def reporte_llenados():
    if not session.get('autenticado'):
        return jsonify({"error": "No autenticado"}), 401
    
    return jsonify({
        "vasos": llenados_vasos,
        "eventos": list(eventos_llenado)
    })

# ============= RUTA PARA EXPORTAR DATOS =============
@app.route("/exportar-csv")
def exportar_csv():
    if not session.get('autenticado'):
        return redirect('/login')
    
    # Crear un archivo CSV en memoria
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Escribir encabezados
    writer.writerow(['Tipo', 'Estado', 'Timestamp', 'Nivel', 'Temperatura'])
    
    # Escribir datos
    for evento in eventos_llenado:
        writer.writerow([
            evento['tipo'],
            evento['estado'],
            evento['timestamp'],
            evento['nivel'],
            evento['temp']
        ])
    
    # Preparar respuesta para descarga
    output.seek(0)
    fecha = datetime.now().strftime("%Y-%m-%d_%H-%M")
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=reporte_llenados_{fecha}.csv"}
    )

# ============= RUTA PARA EXPORTAR PDF =============
@app.route("/exportar-pdf")
def exportar_pdf():
    if not session.get('autenticado'):
        return redirect('/login')
    
    # Crear PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    
    # Título
    pdf.cell(200, 10, txt="Reporte de Eventos de Llenado", ln=1, align='C')
    pdf.ln(10)
    
    # Encabezados de tabla
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(20, 10, "Tipo", 1, 0, 'C')
    pdf.cell(50, 10, "Estado", 1, 0, 'C')
    pdf.cell(50, 10, "Timestamp", 1, 0, 'C')
    pdf.cell(25, 10, "Nivel", 1, 0, 'C')
    pdf.cell(45, 10, "Temperatura", 1, 1, 'C')
    
    pdf.set_font("Arial", size=10)
    for evento in eventos_llenado:
        pdf.cell(20, 10, evento['tipo'], 1, 0, 'C')
        pdf.cell(50, 10, evento['estado'], 1, 0, 'C')
        pdf.cell(50, 10, evento['timestamp'], 1, 0, 'C')
        pdf.cell(25, 10, evento['nivel'], 1, 0, 'C')
        pdf.cell(45, 10, evento['temp'], 1, 1, 'C')
    
    # Preparar respuesta
    fecha = datetime.now().strftime("%Y-%m-%d_%H-%M")
    pdf_output = pdf.output(dest='S').encode('latin1')
    return Response(
        pdf_output,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename=reporte_llenados_{fecha}.pdf"}
    )

# ============= INICIO DEL SISTEMA =============
if __name__ == "__main__":
    threading.Thread(target=conectar_arduino, daemon=True).start()
    time.sleep(1)
    threading.Thread(target=leer_serial, daemon=True).start()
    
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception as e:
        print(f"[WARNING] No se pudo obtener IP local: {e}")
        local_ip = "localhost"
    finally:
        s.close()
    
    print(f"\n{'='*60}")
    print(f"🚀 Sistema de Control de Banda Transportadora")
    print(f"{'='*60}")
    print(f"🌐 Accede desde tu navegador en:")
    print(f"   Local:  http://localhost:5000")
    print(f"   Red:    http://{local_ip}:5000")
    print(f"{'-'*60}")
    print(f"⚠️ Conectando a Arduino...")
    print(f"   Si tienes problemas, verifica el puerto serial")
    print(f"{'='*60}\n")
    
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)