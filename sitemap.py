import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import os
import sys
from typing import List, Tuple
import logging
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import time

# Suppress WebDriver manager logs
os.environ["WDM_LOG"] = "0"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Suppress Selenium and other logs
logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('webdriver_manager').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('chromedriver').setLevel(logging.WARNING)

def validate_xml(url: str) -> Tuple[bool, str]:
    """Validate if the given URL returns a valid XML sitemap."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return False, f"HTTP {response.status_code} received"
        ET.fromstring(response.content)
        return True, "Valid XML"
    except ET.ParseError:
        return False, "Invalid XML structure"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"

def download_sitemap(url: str) -> Tuple[bool, str]:
    """Download the sitemap and save it to a file named after the URL's basename."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return False, f"Failed to download: HTTP {response.status_code}"
        
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            return False, "Invalid filename in URL"
        
        with open(filename, 'wb') as f:
            f.write(response.content)
        return True, f"Saved as {filename}"
    except requests.RequestException as e:
        return False, f"Download error: {str(e)}"
    except OSError as e:
        return False, f"File write error: {str(e)}"

def get_sitemap_urls(sitemap_url: str) -> List[str]:
    """Extract subsitemap URLs from the main sitemap."""
    try:
        response = requests.get(sitemap_url, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to fetch main sitemap: HTTP {response.status_code}")
            return []
        
        # Parse the XML content
        tree = ET.fromstring(response.content)
        
        # Define the namespace
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        
        # Find all sitemap locations
        sitemaps = tree.findall('.//ns:sitemap/ns:loc', namespace)
        
        # Log the number of sitemaps found
        logger.info(f"Found {len(sitemaps)} subsitemaps")
        
        # Extract the URLs
        urls = [loc.text for loc in sitemaps]
        
        # Log the first few URLs for debugging
        if urls:
            logger.info(f"First few subsitemap URLs: {urls[:3]}")
        
        return urls
    except (ET.ParseError, requests.RequestException) as e:
        logger.error(f"Error parsing main sitemap: {str(e)}")
        return []

def get_top_urls(sitemap_url: str, limit: int = 10) -> List[str]:
    """Extract up to 'limit' URLs from a sitemap."""
    try:
        response = requests.get(sitemap_url, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to fetch subsitemap {sitemap_url}: HTTP {response.status_code}")
            return []
        tree = ET.fromstring(response.content)
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        urls = [loc.text for loc in tree.findall('.//ns:url/ns:loc', namespace)]
        return urls[:limit]
    except (ET.ParseError, requests.RequestException) as e:
        logger.error(f"Error parsing subsitemap {sitemap_url}: {str(e)}")
        return []

def check_url_status(urls: List[str]) -> List[Tuple[str, str]]:
    """Check the rendered content for a list of URLs."""
    results = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    # Set up Selenium
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    try:
        for url in urls:
            try:
                # Initial check with requests
                response = requests.get(url, timeout=5, allow_redirects=True, headers=headers)
                status_code = response.status_code
                final_url = response.url
                
                if status_code == 404:
                    results.append((url, "- ERROR 404"))
                    continue
                
                # Use Selenium to render the page
                driver.get(url)
                initial_url = driver.current_url
                
                # Wait for page to stabilize or 404 content
                try:
                    WebDriverWait(driver, 10).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "h1, div:not(:empty)")),
                            EC.title_contains("404"),
                            EC.title_contains("not found"),
                            EC.url_contains("/404")
                        )
                    )
                    # Ensure page is fully loaded
                    WebDriverWait(driver, 5).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                    # Check for URL changes (JS-driven redirects)
                    current_url = driver.current_url
                    if current_url != initial_url and "/404" in current_url.lower():
                        results.append((url, "- ERROR 404"))
                        continue
                except:
                    # Fallback: wait 7 seconds for JS-driven reload
                    time.sleep(7)
                
                rendered_content = driver.page_source
                content_size = len(rendered_content)
                
                # Check for minimal content
                if content_size < 5000 or rendered_content.strip().lower() in ["loading...", ""]:
                    results.append((url, "- ERROR 404"))
                    continue
                
                soup = BeautifulSoup(rendered_content, 'html.parser')
                title = soup.title.string.lower() if soup.title and soup.title.string else ""
                content_lower = rendered_content.lower()
                
                soft_404_indicators = [
                    "404 page not found",
                    "sorry... we seem to have lost this page between our fabric rolls",
                    "page not found",
                    "error 404",
                    "not found"
                ]
                # Check all h1 and div elements
                h1_elements = [h1.string.lower() for h1 in soup.find_all('h1') if h1.string]
                div_elements = [div.string.lower() for div in soup.find_all('div') if div.string]
                
                # Check for generic or suspicious titles
                suspicious_titles = ["loading...", "menu", "", "home"]
                if (title in suspicious_titles or
                    "404" in title or
                    "not found" in title or
                    any(indicator in content_lower for indicator in soft_404_indicators) or
                    any("404" in h1 or "not found" in h1 for h1 in h1_elements) or
                    any(indicator in div for div in div_elements for indicator in soft_404_indicators)):
                    results.append((url, "- ERROR 404"))
                else:
                    results.append((url, "- OK"))
                
                # Save rendered content for debugging
                snippet_file = f"debug_{urlparse(final_url).path.replace('/', '_')}.html"
                with open(snippet_file, 'w', encoding='utf-8') as f:
                    f.write(rendered_content)
                
            except (requests.RequestException, Exception) as e:
                logger.error(f"Error checking {url}: {str(e)}")
                results.append((url, "- ERROR 404"))
    
    finally:
        driver.quit()
    
    return results

def check_sitemap(main_sitemap: str):
    """Main function to check the sitemap, download subsitemaps, and check URLs."""
    logger.info(f"Checking main sitemap: {main_sitemap}")
    
    # Validate main sitemap
    is_valid, message = validate_xml(main_sitemap)
    logger.info(f"Main sitemap - Valid: {is_valid}, Message: {message}")
    if not is_valid:
        logger.error("Main sitemap is invalid, exiting.")
        return

    # Get subsitemap URLs
    subsitemaps = get_sitemap_urls(main_sitemap)
    if not subsitemaps:
        logger.info("No subsitemaps found in the main sitemap.")
        return

    # Check and download each subsitemap
    for subsitemap in subsitemaps:
        logger.info(f"\nChecking subsitemap: {subsitemap}")
        
        # Validate subsitemap XML
        is_valid, message = validate_xml(subsitemap)
        logger.info(f"Subsitemap - Valid: {is_valid}, Message: {message}")
        if not is_valid:
            continue

        # Download subsitemap
        downloaded, download_message = download_sitemap(subsitemap)
        logger.info(f"Download - Success: {downloaded}, Message: {download_message}")
        if not downloaded:
            continue

        # Get top 10 URLs from subsitemap
        urls = get_top_urls(subsitemap)
        if not urls:
            logger.info(f"No URLs found in subsitemap: {subsitemap}")
            continue

        # Check URLs for status
        logger.info(f"Checking top {len(urls)} URLs from {subsitemap}")
        results = check_url_status(urls)
        for url, status_message in results:
            logger.info(f"{url} {status_message}")

if __name__ == "__main__":
    main_sitemap_url = "https://www.acaciafabrics.com/sitemap.xml"
    try:
        check_sitemap(main_sitemap_url)
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        sys.exit(1)