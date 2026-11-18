import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables from .env file
load_dotenv()

supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")

# Initialize the Supabase client
supabase: Client = create_client(supabase_url, supabase_key)

def fetch_news_sources() -> None:
    """Fetches and prints all news sources from the Supabase database."""
    try:
        response = supabase.table("news_sources").select("*").execute()
        print("Successfully connected to Supabase!")
        print("Data retrieved:", response.data)
    except Exception as e:
        print("Error connecting to database:", e)

if __name__ == "__main__":
    fetch_news_sources()