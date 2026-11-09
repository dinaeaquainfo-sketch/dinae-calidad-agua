# Dinae Aqua · Calidad del agua (sincronización con el SINAC)

Este repositorio descarga cada semana los datos públicos de calidad del agua de consumo del
**SINAC** (Sistema de Información Nacional de Agua de Consumo, Ministerio de Sanidad) y los deja en
`data/provincia-29.json`. El plugin de WordPress **Dinae Aqua · Calidad del agua** lee ese fichero una
vez al día y genera las páginas de `dinaeaqua.com/calidad-del-agua/`.

## Qué hace

1. Entra en el acceso ciudadano del SINAC y recorre la provincia de Málaga: municipios, redes de
   distribución y ficha de cada red.
2. De cada red guarda: gestor, población abastecida, origen del agua, tratamientos, precio de
   referencia, lista de boletines (control, completo, radiactividad y vigilancia), el último valor
   notificado de cada parámetro y el detalle de los 3 últimos **análisis completos**.
3. Los boletines no cambian una vez publicados: se guardan en `cache/boletines/` y no se vuelven a
   pedir. `cache/historial/` acumula todos los boletines vistos por red, para poder contar los
   resultados de los últimos 12 meses (el SINAC solo muestra los 10 más recientes de cada tipo).

## Cuándo se ejecuta

- Automáticamente los domingos a las 03:17 (UTC).
- A mano: pestaña **Actions** > **Sincronizar SINAC** > **Run workflow**. En el campo opcional se
  pueden poner códigos INE separados por comas para una prueba rápida (por ejemplo `29084,29094`);
  la prueba se guarda en `data/prueba-29.json` y no toca el fichero de la provincia.

La primera ejecución completa tarda entre una y tres horas, porque el portal del SINAC es lento y el
script espera entre peticiones para no cargarlo. Las siguientes son más rápidas gracias a la caché.

## Ampliar a otra provincia

Duplicar el paso «Descargar datos del SINAC» del workflow cambiando `--ccaa` y `--provincia`
(por ejemplo Córdoba: `--ccaa 1 --provincia 14`) y añadir la nueva dirección `data/provincia-14.json`
en WordPress (Ajustes > Calidad del agua, una dirección por línea).

Códigos de Andalucía: Almería 4, Cádiz 11, Córdoba 14, Granada 18, Huelva 21, Jaén 23, Málaga 29, Sevilla 41.

## Probar en local

```bash
pip install -r sync/requirements.txt
python sync/sinac_sync.py --ccaa 1 --provincia 29 --municipios 29084
```

## Fuente y condiciones

Datos: SINAC, Ministerio de Sanidad. El aviso legal del Ministerio autoriza la reutilización, también
comercial, citando la fuente y la fecha de última actualización y sin desnaturalizar la información.
Las páginas de la web muestran siempre la fuente, la fecha de descarga y el aviso de que no es un
servicio oficial.
