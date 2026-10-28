# Agente de ofertas post-MBA

## 1. Instalación
```bash
pip install requests anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

## 2. Telegram (2 minutos)
1. En Telegram, habla con **@BotFather** → `/newbot` → copia el token.
2. Escríbele cualquier mensaje a tu bot nuevo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el número en `"chat":{"id":...}`.

## 3. Cargar empresas
- Edita `companies.csv` (columnas: `ats,slug,name,priority`). `priority` = `priority` (cada 5 min) o `normal` (cada 30 min).
- Para encontrar slugs automáticamente, haz un archivo `nombres.txt` con un nombre de empresa por línea y corre:
  `python job_agent.py --discover nombres.txt >> companies.csv`
  (revisa el resultado: a veces un slug existe pero es de otra empresa con nombre parecido).
- Pon tus 96 empresas del tracker como `priority`.

## 4. Primera corrida
```bash
python job_agent.py --tier all --dry    # prueba sin enviar nada, mira qué pasaría
python job_agent.py --tier all --seed   # opcional: marca lo existente como visto y solo avisa de lo NUEVO
```
Sin `--seed`, la primera corrida evalúa las ofertas que ya están publicadas (hasta 150 por corrida) y te avisa de las que calzan.

## 5. Automatizar (cron)
```
*/5  * * * * cd /ruta/job_agent && python job_agent.py --tier priority
*/30 * * * * cd /ruta/job_agent && python job_agent.py --tier normal
```
Necesita un equipo prendido siempre (un VPS barato o una Raspberry Pi). GitHub Actions sirve pero tiene mínimo 5 min y puede retrasarse.

## Cómo funciona el filtro
1. **Full-time:** descarta intern, co-op, student, part-time, contract, temporary.
2. **Señal MBA:** debe mencionar MBA, post-MBA, leadership development, rotational, class of 2027, etc.
3. **Visa (exclusión dura):** si el texto dice explícitamente que no patrocina, que exige permanent work authorization o ciudadanía/residencia, se descarta SIN pasar por Claude y queda registrada en `excluded_visa.log` para que puedas auditarla.
4. **Claude:** confirma full-time, EE.UU., fecha de inicio verano 2027 (o "desconocido"), visa, y da un score. Avisa si score >= 6.

Una frase genérica como "must be authorized to work in the US" NO se considera exclusión, porque no dice explícitamente que se requiera autorización permanente.
