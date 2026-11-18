import os
import unicodedata
from dotenv import load_dotenv
from supabase import create_client, Client
from search_engine.scraper import search_google_news

load_dotenv()
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

def normalize_text(text: str) -> str:
    """Removes spaces, converts to lowercase, and strips accents."""
    text = text.lower().replace(" ", "")
    text_normalized = unicodedata.normalize('NFD', text)
    return ''.join(c for c in text_normalized if unicodedata.category(c) != 'Mn')

def get_news_sources_tags() -> dict[str, dict]:
    sources_map = {}
    try:
        # Añadimos 'editorial_group' al select
        response = supabase.table("news_sources").select("domain, tags, region, editorial_group").execute()
        for row in response.data:
            clean_domain = row["domain"].replace(".com", "").replace(".es", "").replace(".cat", "").replace(".eus", "")
            sources_map[clean_domain] = {
                "tags": row["tags"] if row["tags"] else [],
                "region": row["region"] if row["region"] else "Sin clasificar",
                "editorial_group": row["editorial_group"] if row["editorial_group"] else "Sin clasificar"
            }
    except Exception as e:
        print(f"Error fetching sources data: {e}")
    return sources_map

def analyze_event(query: str) -> list[dict]:
    """Searches for an event, assigns tags, and logs unknown sources automatically."""
    tags_map = get_news_sources_tags()
    articles = search_google_news(query)
    analyzed_articles = []
    
    for article in articles:
        source_clean = normalize_text(article["source_name"])
        
        assigned_tags = []
        assigned_region = "Sin clasificar"
        assigned_editorial = "Sin clasificar"
        found = False
        
        for domain_key, data in tags_map.items():
            if domain_key in source_clean:
                assigned_tags = data["tags"]
                assigned_region = data["region"]
                assigned_editorial = data["editorial_group"]
                found = True
                break
        
        # Si el medio no está en la base de datos principal, lo guardamos en pendientes
        if not found:
            try:
                supabase.table("medios_pendientes").upsert(
                    {"source_name": article["source_name"]}, 
                    on_conflict="source_name"
                ).execute()
            except Exception as e:
                print(f"Error logging pending source: {e}")
                
        article["tags"] = assigned_tags
        article["region"] = assigned_region
        article["editorial_group"] = assigned_editorial
        analyzed_articles.append(article)
        
    return analyzed_articles

if __name__ == "__main__":
    test_query = "financiación singular cataluña"
    print(f"Analyzing news for: '{test_query}'...\n")
    
    results = analyze_event(test_query)
    
    # Sort results by the number of tags assigned (just for testing visualization)
    results_sorted = sorted(results, key=lambda x: len(x["tags"]), reverse=True)
    
    for news in results_sorted[:15]:
        tags_display = ", ".join(news['tags']) if news['tags'] else "Sin etiquetar"
        print(f"[{tags_display}] {news['source_name']}")
        print(f"-> {news['title']}\n")