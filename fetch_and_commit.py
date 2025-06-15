# fetch_and_commit.py

import os
import sys
import json
import requests
import gzip
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial

# --- Проверка и импорт зависимостей ---
try:
    from lxml import etree
except ImportError:
    print("Ошибка: lxml не установлен. Установите: pip install lxml", file=sys.stderr)
    sys.exit(1)

try:
    from thefuzz import fuzz
except ImportError:
    print("Ошибка: thefuzz не установлен. Установите: pip install thefuzz python-Levenshtein", file=sys.stderr)
    sys.exit(1)

# --- КОНФИГУРАЦИЯ ОПТИМИЗАЦИИ ---
MAX_WORKERS = 100
SIMILARITY_THRESHOLD = 80

# --- КОНФИГУРАЦИЯ ---
SOURCES_FILE = 'sources.json'
DATA_DIR = Path('data')
ICONS_DIR = Path('icons')
README_FILE = 'README.md'
CHUNK_SIZE = 16 * 1024
MAX_FILE_SIZE_MB = 95

RAW_BASE_URL = "https://raw.githubusercontent.com/{owner}/{repo}/main/{filepath}"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_name(name):
    name = name.lower()
    name = re.sub(r'\s*\b(hd|fhd|uhd|4k|8k|sd|low|vip|\(p\))\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\[.*?\]|\(.*?\)', '', name)
    name = re.sub(r'[^\w\s]', '', name)
    return ' '.join(name.split())

def get_channel_names(channel_element):
    names = [el.text for el in channel_element.findall('display-name')]
    return {clean_name(name) for name in names if name}

def is_gzipped(file_path):
    with open(file_path, 'rb') as f:
        return f.read(2) == b'\x1f\x8b'

# --- ОСНОВНЫЕ ФУНКЦИИ ---

def read_sources_and_notes():
    try:
        with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            sources = config.get('sources', [])
            notes = config.get('notes', '')
            if not sources:
                sys.exit("Ошибка: в sources.json не найдено ни одного источника.")
            for s in sources:
                s.setdefault('ico_src', False)
            return sources, notes
    except FileNotFoundError:
        sys.exit(f"Ошибка: Файл {SOURCES_FILE} не найден.")
    except json.JSONDecodeError:
        sys.exit(f"Ошибка: Некорректный формат JSON в {SOURCES_FILE}.")

def clear_data_dir():
    if DATA_DIR.exists():
        for f in DATA_DIR.iterdir():
            if f.is_file(): f.unlink()
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

def download_one(entry):
    url, desc = entry['url'], entry['desc']
    temp_path = DATA_DIR / ("tmp_" + os.urandom(4).hex())
    result = {'entry': entry, 'error': None}
    try:
        print(f"Начинаю загрузку: {desc} ({url})")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(temp_path, 'wb') as f:
                for chunk in r.iter_content(CHUNK_SIZE): f.write(chunk)
        size_bytes = temp_path.stat().st_size
        size_mb = round(size_bytes / (1024 * 1024), 2)
        if size_bytes == 0: raise ValueError("Файл пустой.")
        if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024: raise ValueError(f"Слишком большой ({size_mb} MB).")
        result.update({'size_mb': size_mb, 'temp_path': temp_path})
        return result
    except Exception as e:
        result['error'] = f"Ошибка загрузки: {e}"
        print(f"Ошибка для {desc}: {result['error']}")
        if temp_path.exists(): temp_path.unlink()
    return result

def download_icon(session, url, save_path):
    try:
        with session.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(8192): f.write(chunk)
        return True
    except requests.RequestException: return False

def _parse_icon_source_file(file_path, desc):
    found_icons = []
    try:
        open_func = gzip.open if is_gzipped(file_path) else open
        with open_func(file_path, 'rb') as f:
            tree = etree.parse(f)
        root = tree.getroot()
        for channel in root.findall('channel'):
            channel_id = channel.get('id')
            icon_tag = channel.find('icon')
            if channel_id and icon_tag is not None and 'src' in icon_tag.attrib:
                icon_url = icon_tag.get('src')
                names = get_channel_names(channel)
                if names and icon_url:
                    found_icons.append((desc, channel_id, names, icon_url))
    except Exception as e:
        print(f"Ошибка парсинга {file_path.name}: {e}", file=sys.stderr)
    return found_icons

def build_icon_database(download_results):
    print("\n--- Этап 1: Создание базы данных иконок ---")
    icon_db, icon_urls_to_download = {}, {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_parse_icon_source_file, r['temp_path'], r['entry']['desc']) for r in download_results if not r.get('error') and r['entry']['ico_src']]
        for future in as_completed(futures):
            for desc, channel_id, names, icon_url in future.result():
                filename = Path(urlparse(icon_url).path).name or f"{channel_id}.png"
                local_icon_path = ICONS_DIR / filename
                db_key = f"{desc}_{channel_id}"
                icon_db[db_key] = {'icon_path': local_icon_path, 'names': names}
                icon_urls_to_download[icon_url] = local_icon_path
    
    print(f"Найдено {len(icon_db)} каналов с иконками. Требуется скачать {len(icon_urls_to_download)} уникальных иконок.")
    adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
    with requests.Session() as session:
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            downloader = partial(download_icon, session)
            future_to_url = {executor.submit(downloader, url, path): url for url, path in icon_urls_to_download.items()}
            for future in as_completed(future_to_url): future.result()
    print("Загрузка иконок завершена.")
    return icon_db

def load_existing_icons():
    print("\n--- Этап 1: Сканирование существующих иконок ---")
    icon_db = {}
    if not ICONS_DIR.is_dir():
        print("Папка icons/ не найдена.")
        return icon_db
    for icon_path in ICONS_DIR.iterdir():
        if icon_path.is_file() and icon_path.suffix.lower() in ['.png', '.jpg', '.jpeg', '.gif']:
            clean_icon_name = clean_name(icon_path.stem)
            icon_db[f"local_{clean_icon_name}"] = {'icon_path': icon_path, 'names': {clean_icon_name}}
    print(f"Найдено {len(icon_db)} существующих иконок.")
    return icon_db

def find_best_match(channel_names, icon_db):
    if not channel_names: return None
    best_match_score = 0
    best_match_path = None
    for db_entry in icon_db.values():
        db_names = db_entry['names']
        if not db_names: continue
        if channel_names & db_names: return db_entry['icon_path']
        score = fuzz.token_set_ratio(' '.join(sorted(channel_names)), ' '.join(sorted(db_names)))
        if score > best_match_score:
            best_match_score = score
            best_match_path = db_entry['icon_path']
    if best_match_score >= SIMILARITY_THRESHOLD:
        return best_match_path
    return None

# <<< ИЗМЕНЕНИЕ 1: Сигнатура функции теперь принимает `entry` >>>
def process_epg_file(file_path, icon_db, owner, repo_name, entry):
    print(f"Обрабатываю файл: {file_path.name}")
    try:
        was_gzipped = is_gzipped(file_path)
        open_func = gzip.open if was_gzipped else open
        parser = etree.XMLParser(remove_blank_text=True)
        with open_func(file_path, 'rb') as f:
            tree = etree.parse(f, parser)
        root = tree.getroot()
        changes_made = 0
        for channel in root.findall('channel'):
            channel_names = get_channel_names(channel)
            matched_icon_path = find_best_match(channel_names, icon_db)
            if matched_icon_path:
                new_icon_url = RAW_BASE_URL.format(owner=owner, repo=repo_name, filepath=matched_icon_path.as_posix())
                icon_tag = channel.find('icon')
                if icon_tag is None:
                    icon_tag = etree.SubElement(channel, 'icon')
                if icon_tag.get('src') != new_icon_url:
                    icon_tag.set('src', new_icon_url)
                    changes_made += 1
        
        if changes_made > 0:
            print(f"Внесено {changes_made} изменений в иконки файла {file_path.name}.")
            doctype_str = '<!DOCTYPE tv SYSTEM "https://iptvx.one/xmltv.dtd">'
            xml_bytes = etree.tostring(tree, pretty_print=True, xml_declaration=True, encoding='UTF-8', doctype=doctype_str)
            
            if was_gzipped:
                # <<< ИЗМЕНЕНИЕ 2: Используем `entry` для получения оригинального имени файла >>>
                original_filename = Path(urlparse(entry['url']).path).name
                archive_internal_name = original_filename[:-3] if original_filename.lower().endswith('.gz') else original_filename
                
                with gzip.GzipFile(filename=archive_internal_name, mode='wb', fileobj=open(file_path, 'wb'), mtime=0) as f_out:
                    f_out.write(xml_bytes)
            else:
                with open(file_path, 'wb') as f_out:
                    f_out.write(xml_bytes)
                    
        return True
    except Exception as e:
        print(f"Критическая ошибка при обработке {file_path}: {e}", file=sys.stderr)
        return False

def update_readme(results, notes):
    utc_now, timestamp = datetime.now(timezone.utc), datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M %Z')
    lines = [notes, "\n---"] if notes else []
    lines.append(f"\n# Обновлено: {timestamp}\n")
    for idx, r in enumerate(results, 1):
        lines.append(f"### {idx}. {r['entry']['desc']}\n")
        if r.get('error'):
            lines.extend([f"**Статус:** 🔴 Ошибка", f"**Источник:** `{r['entry']['url']}`", f"**Причина:** {r.get('error')}"])
        else:
            lines.extend([f"**Размер:** {r['size_mb']} MB", "", f"**Ссылка для плеера (GitHub Raw):**", f"`{r['raw_url']}`"])
        lines.append("\n---")
    with open(README_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"README.md обновлён ({len(results)} записей)")

def main():
    parser = argparse.ArgumentParser(description="EPG Updater Script")
    parser.add_argument('--full-update', action='store_true', help='Perform a full update, including icons.')
    args = parser.parse_args()

    repo = os.getenv('GITHUB_REPOSITORY')
    if not repo or '/' not in repo: sys.exit("Ошибка: не удалось определить GITHUB_REPOSITORY.")
    
    owner, repo_name = repo.split('/')
    sources, notes = read_sources_and_notes()
    clear_data_dir()

    print("\n--- Этап 0: Загрузка EPG файлов ---")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        download_results = list(executor.map(download_one, sources))

    if args.full_update:
        print("\nЗапущен режим ПОЛНОГО ОБНОВЛЕНИЯ (включая иконки).")
        if ICONS_DIR.exists():
            for f in ICONS_DIR.iterdir():
                if f.is_file(): f.unlink()
        else:
            ICONS_DIR.mkdir(parents=True, exist_ok=True)
        icon_db = build_icon_database(download_results)
    else:
        print("\nЗапущен режим ЕЖЕДНЕВНОГО ОБНОВЛЕНИЯ (без загрузки новых иконок).")
        icon_db = load_existing_icons()
    
    print("\n--- Этап 2: Замена ссылок на иконки в EPG файлах ---")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # <<< ИЗМЕНЕНИЕ 3: Передаем `res['entry']` в функцию >>>
        futures = [executor.submit(process_epg_file, res['temp_path'], icon_db, owner, repo_name, res['entry']) for res in download_results if not res.get('error')]
        for future in as_completed(futures): future.result()
    
    print("\n--- Этап 3: Формирование финальных ссылок и README.md ---")
    url_to_result = {res['entry']['url']: res for res in download_results}
    ordered_results = [url_to_result[s['url']] for s in sources]
    final_results, used_names = [], set()

    for res in ordered_results:
        if res.get('error'):
            final_results.append(res)
            continue
        
        final_filename_from_url = Path(urlparse(res['entry']['url']).path).name
        if not Path(final_filename_from_url).suffix:
            proposed_filename = f"{final_filename_from_url}{'.xml.gz' if is_gzipped(res['temp_path']) else '.xml'}"
        else:
            proposed_filename = final_filename_from_url
            
        final_name, counter = proposed_filename, 1
        while final_name in used_names:
            p, stem = Path(proposed_filename), p.name.replace(''.join(p.suffixes), '')
            final_name = f"{stem}-{counter}{''.join(p.suffixes)}"
            counter += 1
        used_names.add(final_name)
        target_path = DATA_DIR / final_name
        res['temp_path'].rename(target_path)
        res['raw_url'] = RAW_BASE_URL.format(owner=owner, repo=repo_name, filepath=target_path.as_posix())
        final_results.append(res)

    update_readme(final_results, notes)
    print("\nСкрипт успешно завершил работу.")

if __name__ == '__main__':
    main()
