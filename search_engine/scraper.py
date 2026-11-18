import urllib.parse
import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime

def search_google_news(query: str) -> list[dict]:
    """
    Searches Google News for a specific topic, returning up to 100 articles
    with formatted datetime objects for filtering.
    """
    encoded_query: str = urllib.parse.quote(query)
    url: str = f"https://news.google.com/rss/search?q={encoded_query}&hl=es-ES&gl=ES&ceid=ES:es"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, "xml")
        articles: list = soup.find_all("item")
        
        results: list[dict] = []
        
        # Modificamos el for para capturar el índice (rango de popularidad)
        for rank, article in enumerate(articles):
            title: str = article.title.text
            link: str = article.link.text
            source: str = article.source.text
            pub_date_raw: str = article.pubDate.text
            
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except Exception:
                pub_date = None
            
            results.append({
                "title": title,
                "link": link,
                "source_name": source,
                "published_date": pub_date,
                "published_date_raw": pub_date_raw,
                "relevance_rank": rank  # NUEVO: Guarda el nivel de popularidad (0 es el más popular)
            })
            
        return results

    except Exception as e:
        print(f"Error fetching news: {e}")
        return []

if __name__ == "__main__":
    test_query = "financiación singular cataluña"
    print(f"Searching news for: '{test_query}'...\n")
    news_results = search_google_news(test_query)
    for i, news in enumerate(news_results[:5], 1):
        print(f"{i}. {news['source_name']} ({news['published_date']}): {news['title']}")