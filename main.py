import discord
from discord.ext import commands
import logging
import urllib.request
from dotenv import load_dotenv
import os
import aiohttp
import asyncio
import zipfile
import io
import re
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

opener = urllib.request.build_opener()
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"),
    ("Accept", "*/*"),
    ("Accept-Language", "en-US,en;q=0.9"),
]

# Install the opener so urlretrieve uses the same headers
urllib.request.install_opener(opener)

# Load environment variables from .env file
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Set up logging
handler = logging.FileHandler(filename='bot.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
intents.members = True  # Enable members intent

# Create bot instance
bot = commands.Bot(command_prefix='/', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

export_lock = asyncio.Lock()

async def fetch_bytes(session, url, max_retries=3, backoff_factor=1.0, timeout_seconds=10):
    """Fetch bytes from a URL with retries, exponential backoff, a browser UA, and timeout.

    Returns bytes on success or None on failure / non-image responses.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with session.get(url, headers=headers, timeout=timeout) as response:
                if response.status != 200:
                    last_exc = f"HTTP {response.status}"
                    raise Exception(last_exc)

                content_type = response.headers.get("Content-Type", "")
                # If it's already an image, return bytes
                if content_type.startswith("image/"):
                    return await response.read()

                # If HTML, try to extract og:image and fetch that instead
                if "text/html" in content_type:
                    text = await response.text()
                    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', text, re.I)
                    if m:
                        image_url = m.group(1)
                        try:
                            async with session.get(image_url, headers=headers, timeout=timeout) as img_resp:
                                if img_resp.status == 200 and img_resp.headers.get("Content-Type", "").startswith("image/"):
                                    return await img_resp.read()
                                last_exc = f"og:image fetch HTTP {img_resp.status}"
                        except Exception as e:
                            last_exc = str(e)
                    # otherwise not an image we can use
                    last_exc = "HTML page without usable og:image"
                    raise Exception(last_exc)

                # Not an image or HTML we can parse
                last_exc = f"Unsupported Content-Type: {content_type}"
                raise Exception(last_exc)

        except asyncio.TimeoutError:
            last_exc = "timeout"
        except Exception as e:
            # record and retry if allowed
            last_exc = str(e)

        # Backoff before next attempt
        if attempt < max_retries:
            await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))

    # All retries exhausted
    print(f"Failed to fetch {url}: {last_exc}")
    return None

@bot.command()
async def imgexport(ctx):
    if export_lock.locked():
        await ctx.send("An export is already running.")
        return

    async with export_lock:
        await ctx.send("Starting image export... This may take a while.")

        image_count = 0
        zip_buffer = io.BytesIO()

        async with aiohttp.ClientSession() as session:
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:

                # Collect messages into a list first so we can report progress.
                # Note: this will load all messages into memory; if the channel is huge
                # consider processing in a single pass and emitting coarse progress instead.
                messages = [m async for m in ctx.channel.history(limit=None)]
                # Track any URLs we intentionally skipped or that failed to fetch
                skipped_urls = []
                total_messages = len(messages)
                print(f"Total messages to scan: {total_messages}")
                i = 0
                for message in messages:
                    i += 1
                    # Log progress roughly every 5% or at least every 100 messages
                    if total_messages and (i % max(1, total_messages // 20) == 0 or i % 100 == 0):
                        percent = i / total_messages * 100
                        print(f"Progress: {percent:.1f}% ({i}/{total_messages})")
                    # Attachments
                    for attachment in message.attachments:
                        # Skip Instagram links (they often point to HTML pages, not direct images)
                        if attachment.url and "instagram.com" in attachment.url:
                            msg = f"{attachment.url} - skipped: instagram"
                            print(f"Skipping Instagram attachment URL: {attachment.url}")
                            skipped_urls.append(msg)
                            continue

                        # Heuristics to decide if this is likely an image even when content_type is missing
                        likely_image = False
                        filename_ext = None
                        if attachment.filename:
                            lf = attachment.filename.lower()
                            if lf.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff')):
                                likely_image = True
                                filename_ext = os.path.splitext(attachment.filename)[1]

                        if not likely_image and attachment.url:
                            if re.search(r"\.(png|jpe?g|gif|webp|bmp|tiff)(?:\?|$)", attachment.url, re.I):
                                likely_image = True
                                ext_match = re.search(r"\.(png|jpe?g|gif|webp|bmp|tiff)(?:\?|$)", attachment.url, re.I)
                                if ext_match:
                                    filename_ext = '.' + ext_match.group(1).lower()

                        # If content_type explicitly says image, treat as image
                        if getattr(attachment, 'content_type', None) and attachment.content_type.startswith('image'):
                            likely_image = True

                        data = None
                        used_ext = filename_ext

                        # If we think it's an image, or are unsure, attempt to fetch and verify
                        if likely_image or True:
                            data = await fetch_bytes(session, attachment.url)

                        if not data:
                            skipped_urls.append(f"{attachment.url} - fetch failed or not an image")
                            continue

                        # If we still don't have an extension, try to infer from Content-Type by peeking
                        # (fetch_bytes already checked Content-Type; infer extension from URL or filename as fallback)
                        if not used_ext:
                            # try filename first
                            if attachment.filename and os.path.splitext(attachment.filename)[1]:
                                used_ext = os.path.splitext(attachment.filename)[1]
                            else:
                                # fallback to jpg
                                used_ext = '.jpg'

                        safe_filename = attachment.filename if getattr(attachment, 'filename', None) else f"{message.id}{used_ext}"
                        filename = f"{message.id}_{safe_filename}"
                        zipf.writestr(filename, data)
                        image_count += 1

                    # Embedded images
                    for embed in message.embeds:
                        # prefer embed.image.url but fall back to embed.url
                        url_to_check = None
                        if getattr(embed, 'image', None) and getattr(embed.image, 'url', None):
                            url_to_check = embed.image.url
                        elif getattr(embed, 'url', None):
                            url_to_check = embed.url

                        if url_to_check and "instagram.com" in url_to_check:
                            print(f"Skipping Instagram embed URL: {url_to_check}")
                            skipped_urls.append(f"{url_to_check} - skipped: instagram")
                            continue

                        if not url_to_check:
                            continue

                        # If the URL looks like an image by extension, or embed.image exists, try to fetch
                        if re.search(r"\.(png|jpe?g|gif|webp|bmp|tiff)(?:\?|$)", url_to_check, re.I) or getattr(embed, 'image', None):
                            data = await fetch_bytes(session, url_to_check)
                            if data:
                                # derive extension
                                ext_match = re.search(r"\.(png|jpe?g|gif|webp|bmp|tiff)(?:\?|$)", url_to_check, re.I)
                                used_ext = '.' + ext_match.group(1).lower() if ext_match else '.jpg'
                                filename = f"{message.id}_embed_{image_count}{used_ext}"
                                zipf.writestr(filename, data)
                                image_count += 1
                            else:
                                skipped_urls.append(f"{url_to_check} - fetch failed or not an image")

                # If any URLs were skipped or failed, include a text report inside the ZIP
                if skipped_urls:
                    report = "Skipped or failed URLs:\n" + "\n".join(skipped_urls)
                    zipf.writestr("skipped_urls.txt", report)

        zip_buffer.seek(0)

        # Also save the ZIP to disk locally in an `exports/` folder with a timestamped name
        try:
            os.makedirs("exports", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_filename = f"channel_{ctx.channel.id}_{timestamp}_images.zip"
            local_path = os.path.join("exports", local_filename)
            with open(local_path, "wb") as f:
                f.write(zip_buffer.getvalue())
            print(f"Saved local copy of export to: {local_path}")
        except Exception as e:
            print(f"Failed to save local copy of ZIP: {e}")

        if image_count == 0:
            await ctx.send("No images found in this channel.")
            return

        await ctx.send(
            content=f"Export complete. {image_count} images archived.",
        )

@bot.command()
async def smgexport(ctx):
    """Export only messages sent by the invoking user in this channel as a .txt file.

    The file will contain a simple timestamped list of messages and will be saved
    locally under `exports/` and also sent to the channel as an attachment.
    """
    if export_lock.locked():
        await ctx.send("An export is already running.")
        return

    async with export_lock:
        await ctx.send("Starting export of your messages... This may take a moment.")

        # Collect messages authored by the invoking user
        messages = [m async for m in ctx.channel.history(limit=None) if m.author.id == ctx.author.id]

        if not messages:
            await ctx.send("No messages from you found in this channel.")
            return

        # Build text content (chronological order: oldest first)
        messages.reverse()
        lines = []
        for m in messages:
            ts = m.created_at.isoformat() if getattr(m, 'created_at', None) else ''
            content = m.content or ''
            lines.append(f"[{ts}] {content}")

        text_data = "\n\n".join(lines)

        # Create BytesIO and write utf-8 text
        txt_buffer = io.BytesIO()
        txt_buffer.write(text_data.encode('utf-8'))
        txt_buffer.seek(0)

        # Save a local copy
        try:
            os.makedirs("exports", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_filename = f"messages_{ctx.author.id}_{ctx.channel.id}_{timestamp}.txt"
            local_path = os.path.join("exports", local_filename)
            with open(local_path, "wb") as f:
                f.write(txt_buffer.getvalue())
            print(f"Saved local copy of messages export to: {local_path}")
        except Exception as e:
            print(f"Failed to save local copy of messages export: {e}")

        # Reset buffer pointer before sending
        txt_buffer.seek(0)

        await ctx.send(content=f"Export complete. {len(messages)} messages exported.", file=discord.File(txt_buffer, filename=local_filename))

@bot.command()
async def getevents(ctx):
    xml = urllib.request.urlopen("https://hakkerikoda.ee/et/rss.xml")

    root = ET.fromstring(xml.read())
    namespaces = {"ev": "http://purl.org/rss/1.0/modules/event/"}
    await ctx.send("Here are the latest events from hakkerikoda.ee:")
    event_count = 0

    for item in root.findall(".//item")[:5]:  # Limit to the first 5 events
        title = item.findtext("title", default="Untitled")
        link = item.findtext("link", default="")
        pub_date = item.findtext("pubDate", default="Unknown")
        start_date_text = item.findtext("ev:startdate", default="", namespaces=namespaces)
        location = item.findtext("ev:location", default="Unknown", namespaces=namespaces)

        if not start_date_text:
            continue  # Skip events without a start date

        start_date = datetime.fromisoformat(start_date_text)
        if start_date.date() <= datetime.now().date() - timedelta(days=1):
            continue

        await ctx.send(
            f"**{title}**\nPublished on: {pub_date}\n"
            f"Start date: {start_date_text}\nLocation: {location}\nLink: {link}"
        )
        event_count += 1

    if event_count == 0:
        await ctx.send("No upcoming events found.")

    



bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)