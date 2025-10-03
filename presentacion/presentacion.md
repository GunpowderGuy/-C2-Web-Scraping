---
marp: true
title: Trabajo C2 – Web Scraping (Wong)
paginate: true
---

# 🕸️ Trabajo C2 – Web Scraping  
**Caso:** Descubrimiento de PDPs en Wong  
**Código base:** `piton3.py`  
**Equipo:** [Integrantes] · **Curso:** [C2] · **Docente:** [Nombre]

---

## 🎯 Objetivo del trabajo
- Aplicar *web scraping* para **descubrir y recolectar** URLs de páginas de producto (PDP) desde un e-commerce.  
- Generar un **dataset reproducible** (CSV) a partir de *sitemaps* oficiales.  
- Presentar **metodología, resultados y buenas prácticas** alineadas a la **rúbrica** de la tarea.

---

## 🧰 Herramientas y entorno
- **Python 3**  
- **Librerías:** `selenium`, `beautifulsoup4`, `pandas`, `requests`  
- **Navegador/Driver:** **Chrome** + **ChromeDriver** (modo *headless*)  
- **Salida:** `wong_pdp_urls.csv`

> En clase/consigna (semana 6) se indicó un **.ipynb con 2 celdas**:  
> 1) instalación/imports, 2) solución (scraping).  
> Este flujo está cubierto en la presentación y el repo.

---

## 🏗️ Arquitectura del scraper (`piton3.py`)
**Funciones clave observadas:**
- `run_from_sitemaps(...)` → orquesta el flujo de descubrimiento:
  - parámetros vistos: `target_total=1000`, `url_dump_csv="wong_pdp_urls.csv"`
- `mk_driver(headless=...)` → inicializa **Selenium/Chrome** en modo *headless*.

**Flujo general:**
1) Descargar y parsear **sitemaps**  
2) Extraer **PDP URLs**  
3) Volcar en **CSV** (dump)  
4) (Extensible) visitar PDPs y extraer atributos

---

## 🔁 Flujo paso a paso (alto nivel)
1. **Descarga sitemaps** del dominio objetivo  
2. **Parseo** de los nodos `<loc>`  
3. **Filtrado** por patrón de PDP (producto)  
4. **Acumulación** de URLs únicas  
5. **Persistencia** en `wong_pdp_urls.csv`  
6. **(Opcional)** scraping por PDP: precio, stock, categoría

---

## 🧪 Evidencia de ejecución (logs de `piton3.py`)
- Mensajes emitidos por el script:
  - `[DISCOVERY] PDP encontradas: 28910`  
  - `[DUMP] wong_pdp_urls.csv (todas las PDP descubiertas)`

> Se confirma **descubrimiento masivo** y **persistencia** en CSV.

---

## 📈 Resultados
- **Total PDPs encontradas:** **28,910**  
- **Archivo generado:** `wong_pdp_urls.csv`  
- **Ejemplo (formato):**  

