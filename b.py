import logging
import re
import aiohttp
import aiofiles
import hashlib
import json
import os
import requests
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import AndTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
import pytz
import asyncio

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
USER_DATA_FILE = 'user_data.json'
CHANNELS_FILE = 'authorized_channels.json'
SUDO_USERS_FILE = 'sudo_users.json'
SUPERGROUPS_FILE = 'authorized_supergroups.json'
OWNER_ID = 6556141430  # Replace with your Telegram user ID
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45MB
CHECK_INTERVAL = 30  # Minutes
DEFAULT_TZ = pytz.timezone("Asia/Kolkata")

# Supported file types
DOCUMENT_EXTS = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt']
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']
AUDIO_EXTS = ['.mp3', '.wav', '.ogg']
VIDEO_EXTS = ['.mp4', '.mov', '.avi', '.mkv']
ALLOWED_EXTS = DOCUMENT_EXTS + IMAGE_EXTS + AUDIO_EXTS + VIDEO_EXTS

# Scheduler configuration
jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}
job_defaults = {
    'misfire_grace_time': 3600,
    'coalesce': True,
    'max_instances': 1
}

scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults, timezone=DEFAULT_TZ)

# Helper functions for data management
def load_json(file):
    try:
        with open(file, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return [] if 'channel' in file or 'supergroup' in file else {}

def save_json(data, file):
    with open(file, 'w') as f:
        json.dump(data, f, indent=4)

authorized_channels = load_json(CHANNELS_FILE)
sudo_users = load_json(SUDO_USERS_FILE)
authorized_supergroups = load_json(SUPERGROUPS_FILE)
user_data = load_json(USER_DATA_FILE)

# Authorization filter
def is_authorized(_, __, message: Message):
    if message.chat.type == enums.ChatType.PRIVATE:
        return str(message.from_user.id) in sudo_users or message.from_user.id == OWNER_ID
    elif message.chat.type == enums.ChatType.CHANNEL:
        return str(message.chat.id) in authorized_channels
    elif message.chat.type == enums.ChatType.SUPERGROUP:
        return str(message.chat.id) in authorized_supergroups
    return False

# File handling
async def download_file(url, custom_name=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                
                # Check file size before download
                content_length = int(response.headers.get('Content-Length', 0))
                if content_length > MAX_FILE_SIZE:
                    return None
                
                # Determine file extension
                ext = os.path.splitext(urlparse(url).path)[1].lower()
                content_type = response.headers.get('Content-Type', '')
                if not ext:
                    if 'image' in content_type:
                        ext = '.jpg'
                    elif 'audio' in content_type:
                        ext = '.mp3'
                    elif 'video' in content_type:
                        ext = '.mp4'
                    else:
                        ext = '.bin'

                filename = re.sub(r'[\\/*?:"<>|]', '', (custom_name or os.path.basename(urlparse(url).path)) + ext)
                async with aiofiles.open(filename, 'wb') as f:
                    await f.write(await response.read())
                    if os.path.getsize(filename) > MAX_FILE_SIZE:
                        os.remove(filename)
                        return None
                    return filename
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None

# HTML parsing
def extract_files(html, base_url):
    soup = BeautifulSoup(html, 'lxml')
    files = []
    for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
        url = tag.get('href') or tag.get('src')
        if not url:
            continue
        url = urljoin(base_url, url)
        if not any(url.lower().endswith(ext) for ext in ALLOWED_EXTS):
            continue
        
        name = tag.get('alt') or tag.get('title') or os.path.basename(url)
        file_type = 'document'
        ext = os.path.splitext(url)[1].lower()
        if ext in IMAGE_EXTS:
            file_type = 'image'
        elif ext in AUDIO_EXTS:
            file_type = 'audio'
        elif ext in VIDEO_EXTS:
            file_type = 'video'
        
        files.append({'name': name, 'url': url, 'type': file_type})
    return files

# Website monitoring
async def check_single_website(client: Client, url: str, user_id: int):
    try:
        response = requests.get(url)
        response.raise_for_status()
        current_hash = hashlib.sha256(response.content).hexdigest()
        
        user_str = str(user_id)
        if user_str not in user_data or url not in user_data[user_str]['tracked_urls']:
            return
        
        tracked = user_data[user_str]['tracked_urls'][url]
        if current_hash == tracked['hash']:
            return
        
        # Process updates
        new_files = extract_files(response.text, url)
        previous_files = tracked.get('files', [])
        tracked['hash'] = current_hash
        tracked['files'] = [f['url'] for f in new_files]
        save_json(user_data, USER_DATA_FILE)
        
        # Generate and send updates
        summary = []
        for file in new_files:
            if file['url'] not in previous_files:
                filename = await download_file(file['url'], file['name'])
                if filename:
                    await client.send_document(user_id, filename, caption=f"{file['name']} ({file['type']})")
                    os.remove(filename)
                    summary.append(f"{file['name']} ({file['type']})")
        
        if summary:
            summary_text = "New files:\n" + "\n".join(summary)
            await client.send_message(user_id, f"Website updated: {url}\n{summary_text}")
    except Exception as e:
        logger.error(f"Monitoring error: {e}")

# Bot commands
async def start(client: Client, message: Message):
    await message.reply_text(
        "Bot Commands:\n"
        "/track <url> <interval> [night]\n"
        "/untrack <url>\n"
        "/list\n"
        "/documents <url>\n"
        "/addchannel <id> (Owner)\n"
        "/removechannel <id> (Owner)\n"
        "/addsupergroup <id> (Owner)\n"
        "/removesupergroup <id> (Owner)\n"
        "/addsudo <id> (Owner)\n"
        "/removesudo <id> (Owner)"
    )

async def track(client: Client, message: Message):
    try:
        args = message.text.split()
        url, interval = args[1], int(args[2])
        night_mode = 'night' in args[3:]
        
        # Schedule job
        trigger = IntervalTrigger(minutes=interval)
        if night_mode:
            trigger = AndTrigger([trigger, CronTrigger(hour='6-22')])
        
        job_id = f"{message.chat.id}_{hashlib.md5(url.encode()).hexdigest()[:6]}"
        scheduler.add_job(
            check_single_website,
            trigger=trigger,
            args=[client, url, message.chat.id],
            id=job_id,
            replace_existing=True
        )
        
        # Update user data
        user_data.setdefault(str(message.chat.id), {}).setdefault('tracked_urls', {})[url] = {
            'hash': '',
            'interval': interval,
            'night_mode': night_mode,
            'files': []
        }
        save_json(user_data, USER_DATA_FILE)
        await message.reply_text(f"Now tracking {url} every {interval} minutes")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def untrack(client: Client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /untrack <url>")
            return
        
        url = args[1]
        user_str = str(message.chat.id)
        
        if user_str not in user_data or url not in user_data[user_str]['tracked_urls']:
            await message.reply_text(f"Not tracking {url}")
            return
        
        # Remove the job from the scheduler
        job_id = f"{message.chat.id}_{hashlib.md5(url.encode()).hexdigest()[:6]}"
        scheduler.remove_job(job_id)
        
        # Remove the URL from user data
        del user_data[user_str]['tracked_urls'][url]
        save_json(user_data, USER_DATA_FILE)
        
        await message.reply_text(f"Stopped tracking {url}")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def list_urls(client: Client, message: Message):
    try:
        user_str = str(message.chat.id)
        if user_str not in user_data or not user_data[user_str]['tracked_urls']:
            await message.reply_text("You are not tracking any URLs.")
            return
        
        tracked_urls = user_data[user_str]['tracked_urls']
        response = "Tracked URLs:\n" + "\n".join(
            f"{url} (every {info['interval']} mins, night mode: {'ON' if info['night_mode'] else 'OFF'})"
            for url, info in tracked_urls.items()
        )
        await message.reply_text(response)
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def documents(client: Client, message: Message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /documents <url>")
            return
        
        url = args[1]
        response = requests.get(url)
        response.raise_for_status()
        
        files = extract_files(response.text, url)
        if not files:
            await message.reply_text("No files found on this website.")
            return
        
        response_text = "Files found:\n" + "\n".join(
            f"{file['name']} ({file['type']}): {file['url']}"
            for file in files
        )
        await message.reply_text(response_text)
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def addchannel(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /addchannel <channel_id>")
            return
        
        channel_id = args[1]
        if channel_id in authorized_channels:
            await message.reply_text("Channel is already authorized.")
            return
        
        authorized_channels.append(channel_id)
        save_json(authorized_channels, CHANNELS_FILE)
        await message.reply_text(f"Added channel {channel_id} to authorized channels.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def removechannel(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /removechannel <channel_id>")
            return
        
        channel_id = args[1]
        if channel_id not in authorized_channels:
            await message.reply_text("Channel is not authorized.")
            return
        
        authorized_channels.remove(channel_id)
        save_json(authorized_channels, CHANNELS_FILE)
        await message.reply_text(f"Removed channel {channel_id} from authorized channels.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def addsupergroup(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /addsupergroup <group_id>")
            return
        
        group_id = args[1]
        if group_id in authorized_supergroups:
            await message.reply_text("Supergroup is already authorized.")
            return
        
        authorized_supergroups.append(group_id)
        save_json(authorized_supergroups, SUPERGROUPS_FILE)
        await message.reply_text(f"Added supergroup {group_id} to authorized supergroups.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def removesupergroup(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /removesupergroup <group_id>")
            return
        
        group_id = args[1]
        if group_id not in authorized_supergroups:
            await message.reply_text("Supergroup is not authorized.")
            return
        
        authorized_supergroups.remove(group_id)
        save_json(authorized_supergroups, SUPERGROUPS_FILE)
        await message.reply_text(f"Removed supergroup {group_id} from authorized supergroups.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def addsudo(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /addsudo <user_id>")
            return
        
        user_id = args[1]
        if user_id in sudo_users:
            await message.reply_text("User is already a sudo user.")
            return
        
        sudo_users.append(user_id)
        save_json(sudo_users, SUDO_USERS_FILE)
        await message.reply_text(f"Added user {user_id} to sudo users.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def removesudo(client: Client, message: Message):
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("Only the owner can use this command.")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /removesudo <user_id>")
            return
        
        user_id = args[1]
        if user_id not in sudo_users:
            await message.reply_text("User is not a sudo user.")
            return
        
        sudo_users.remove(user_id)
        save_json(sudo_users, SUDO_USERS_FILE)
        await message.reply_text(f"Removed user {user_id} from sudo users.")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

# Update main() to include all handlers
def main():
    app = Client("my_bot",
                 api_id=os.getenv("API_ID"),
                 api_hash=os.getenv("API_HASH"),
                 bot_token=os.getenv("BOT_TOKEN"))
    
    # Add all handlers with authorization
    app.add_handler(MessageHandler(start, filters.command("start") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(track, filters.command("track") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(untrack, filters.command("untrack") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(list_urls, filters.command("list") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(documents, filters.command("documents") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(addchannel, filters.command("addchannel") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(removechannel, filters.command("removechannel") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(addsupergroup, filters.command("addsupergroup") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(removesupergroup, filters.command("removesupergroup") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(addsudo, filters.command("addsudo") & filters.create(is_authorized)))
    app.add_handler(MessageHandler(removesudo, filters.command("removesudo") & filters.create(is_authorized)))
    
    # Start the bot and scheduler
    async def run():
        await app.start()
        scheduler.start()
        await asyncio.Event().wait()  # Keep the bot running

    # Run the bot
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        scheduler.shutdown()
        app.stop()

if __name__ == "__main__":
    main()
