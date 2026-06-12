@echo off
title Trading Pro — Sistema Completo
cd /d "%~dp0"

echo ======================================
echo  TRADING PRO — SISTEMA COMPLETO
echo  XAUUSD Signal Engine
echo ======================================
echo.
echo Iniciando data engine por primera vez...
py data_engine.py
echo.
echo Sistema iniciado. Comenzando ciclo automatico...
echo.

set DATA_COUNT=0
set SIGNAL_COUNT=0
set TRACKER_COUNT=0

:loop
echo ======================================
echo  CICLO: %date% %time%
echo ======================================

REM DATA ENGINE — cada 60 segundos (cada 4 ciclos de 15s)
set /a DATA_COUNT+=1
if %DATA_COUNT% GEQ 4 (
    echo [DATA] Actualizando velas MT5...
    py data_engine.py
    set DATA_COUNT=0
)

REM SIGNAL ENGINE — cada 30 segundos (cada 2 ciclos de 15s)
set /a SIGNAL_COUNT+=1
if %SIGNAL_COUNT% GEQ 2 (
    echo [SIGNAL] Analizando senales...
    py signal_engine.py
    set SIGNAL_COUNT=0
)

REM RESULT TRACKER — cada 60 segundos (cada 4 ciclos de 15s)
set /a TRACKER_COUNT+=1
if %TRACKER_COUNT% GEQ 4 (
    echo [TRACKER] Revisando resultados...
    py result_tracker.py
    set TRACKER_COUNT=0
)

echo.
echo Esperando 15 segundos...
timeout /t 15 /nobreak
goto loop
