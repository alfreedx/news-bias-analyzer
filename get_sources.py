import time
from search_engine.scraper import search_google_news

keywords = ["españa", "gobierno", "sucesos", "política", "economía", "sociedad", "madrid", "cataluña", "andalucía"]
discovered_sources = set()

print("Rastreando Google News en busca de medios españoles...\n")

for word in keywords:
    print(f"Buscando tema: '{word}'...")
    articles = search_google_news(word)
    for article in articles:
        discovered_sources.add(article["source_name"])
    time.sleep(2) # Pausa para no saturar a Google

print(f"\n¡Se han encontrado {len(discovered_sources)} medios distintos!")
for source in sorted(list(discovered_sources)):
    print(f"- {source}")