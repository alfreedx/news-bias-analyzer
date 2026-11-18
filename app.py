import streamlit as st
import math
from search_engine.engine import analyze_event

# 1. Configuración de página e inyección de CSS para diseño y colores
st.set_page_config(page_title="Buscador de Sucesos", page_icon="📰", layout="wide")

st.markdown("""
    <style>
    .tag-match { background-color: #2e7d32; color: white; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin-right: 5px; display: inline-block;}
    .tag-normal { background-color: #555555; color: white; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin-right: 5px; display: inline-block;}
    .news-card { padding: 15px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 15px; }
    </style>
""", unsafe_allow_html=True)

# 2. Inicialización del estado de la sesión (Memoria)
if "all_results" not in st.session_state:
    st.session_state.all_results = []
if "current_page" not in st.session_state:
    st.session_state.current_page = 1
if "search_query" not in st.session_state:
    st.session_state.search_query = ""

st.title("Buscador analítico de sucesos")

# 3. Buscador principal
with st.form(key="search_form"):
    query = st.text_input("¿Qué suceso quieres buscar?", value=st.session_state.search_query)
    submit_button = st.form_submit_button("Buscar Noticias")

if submit_button:
    if query:
        with st.spinner("Extrayendo noticias y analizando sesgos..."):
            st.session_state.all_results = analyze_event(query)
            st.session_state.current_page = 1
            st.session_state.search_query = query
            st.rerun()

# 4. Panel de Filtros y Visualización (Pegado al margen izquierdo)
if st.session_state.all_results:
    all_tags = set()
    all_regions = set()
    all_editorials = set()
    all_sources = set() # Nueva variable para los medios
    
    for news in st.session_state.all_results:
        all_regions.add(news.get("region", "Sin clasificar"))
        all_editorials.add(news.get("editorial_group", "Sin clasificar"))
        all_sources.add(news.get("source_name", "Desconocido"))
        for tag in news["tags"]:
            all_tags.add(tag)
    
    st.markdown("### Filtros y Ordenación")
    
    # Primera fila de filtros (4 columnas)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        selected_tags = st.multiselect("Etiquetas ideológicas:", options=sorted(list(all_tags)))
    with col2:
        selected_regions = st.multiselect("Territorio:", options=sorted(list(all_regions)))
    with col3:
        selected_editorials = st.multiselect("Grupo Editorial:", options=sorted(list(all_editorials)))
    with col4:
        selected_sources = st.multiselect("Medio de comunicación:", options=sorted(list(all_sources)))
        
    # Segunda fila (Tiempo y Ordenación)
    col5, col6 = st.columns(2)
    with col5:
        date_range = st.date_input("Rango de fechas:", value=[])
    with col6:
        sort_option = st.selectbox("Ordenar por:", options=["Relevancia (Popularidad)", "Más recientes primero", "Más antiguos primero"])

    # 5. Aplicar la lógica de filtrado combinada
    filtered_results = []
    for news in st.session_state.all_results:
        if selected_tags and not any(tag in selected_tags for tag in news["tags"]):
            continue
        if selected_regions and news.get("region") not in selected_regions:
            continue
        if selected_editorials and news.get("editorial_group") not in selected_editorials:
            continue
        if selected_sources and news.get("source_name") not in selected_sources: # Nuevo filtro
            continue
        if len(date_range) == 2 and news["published_date"]:
            start_date, end_date = date_range
            news_date = news["published_date"].date()
            if not (start_date <= news_date <= end_date):
                continue
                
        filtered_results.append(news)

    # 5.5 Aplicar la lógica de ordenación
    if sort_option == "Más recientes primero":
        filtered_results.sort(key=lambda x: x["published_date"].timestamp() if x["published_date"] else 0, reverse=True)
    elif sort_option == "Más antiguos primero":
        filtered_results.sort(key=lambda x: x["published_date"].timestamp() if x["published_date"] else float('inf'))
    else:
        filtered_results.sort(key=lambda x: x.get("relevance_rank", 0))

    st.divider()

    # 6. Paginación y Renderizado
    ITEMS_PER_PAGE = 20
    total_pages = math.ceil(len(filtered_results) / ITEMS_PER_PAGE) if filtered_results else 1
    
    if not filtered_results:
        st.info("No hay noticias que coincidan con los filtros actuales.")
    else:
        if st.session_state.current_page > total_pages:
            st.session_state.current_page = total_pages
            
        start_idx = (st.session_state.current_page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        
        st.markdown(f"**Mostrando {len(filtered_results)} resultados en total:**")
        
        for news in filtered_results[start_idx:end_idx]:
            date_str = news["published_date"].strftime("%d/%m/%Y %H:%M") if news["published_date"] else "Fecha desconocida"
            
            tags_html = ""
            for tag in news["tags"]:
                if tag in selected_tags:
                    tags_html += f'<span class="tag-match">{tag}</span>'
                else:
                    tags_html += f'<span class="tag-normal">{tag}</span>'
            
            if not tags_html:
                tags_html = '<span class="tag-normal">Sin clasificar</span>'
            
            st.markdown(f"""
            <div class="news-card">
                <h4><a href="{news['link']}" target="_blank" style="text-decoration:none; color: #1E88E5;">{news['title']}</a></h4>
                    <p style="margin-bottom: 8px; font-size: 14px; color: #888;">
                        <strong>{news['source_name']}</strong> ({news.get('region', 'Sin clasificar')} | {news.get('editorial_group', 'Sin clasificar')}) • {date_str}
                    </p>
                <div>{tags_html}</div>
            </div>
            """, unsafe_allow_html=True)
        
        st.write("")
        col_prev, col_page, col_next = st.columns([1, 2, 1])
        
        with col_prev:
            if st.session_state.current_page > 1:
                if st.button("⬅️ Anterior"):
                    st.session_state.current_page -= 1
                    st.rerun()
        
        with col_page:
            st.markdown(f"<div style='text-align: center;'><strong>Página {st.session_state.current_page} de {total_pages}</strong></div>", unsafe_allow_html=True)
        
        with col_next:
            if st.session_state.current_page < total_pages:
                if st.button("Siguiente ➡️"):
                    st.session_state.current_page += 1
                    st.rerun()