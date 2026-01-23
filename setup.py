#!/usr/bin/env python3
"""
Скрипт автоматической установки зависимостей для KIBER SOS Bot
"""
import subprocess
import sys
import os
import platform

def run_command(cmd, shell=False):
    """Выполняет команду и выводит результат"""
    try:
        result = subprocess.run(
            cmd if not shell else cmd,
            shell=shell,
            check=True,
            capture_output=True,
            text=True
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr
    except FileNotFoundError:
        return False, "Команда не найдена"

def check_python_version():
    """Проверяет версию Python"""
    version = sys.version_info
    print(f"🐍 Python версия: {version.major}.{version.minor}.{version.micro}")
    
    if version.major < 3 or (version.major == 3 and version.minor < 11):
        print("⚠️ Рекомендуется Python 3.11+")
    else:
        print("✅ Версия Python подходит")
    print()

def check_node():
    """Проверяет установлен ли Node.js"""
    print("🔍 Проверка Node.js...")
    success, output = run_command(["node", "--version"])
    
    if success:
        print(f"✅ Node.js установлен: {output.strip()}")
        return True
    else:
        print("❌ Node.js не установлен")
        print("📥 Установите: https://nodejs.org/")
        return False

def install_copilot_cli():
    """Устанавливает GitHub Copilot CLI"""
    print("\n📦 Установка GitHub Copilot CLI...")
    
    success, output = run_command(["npm", "install", "-g", "@github/copilot"])
    
    if success:
        print("✅ Copilot CLI установлен")
        return True
    else:
        print(f"❌ Ошибка установки: {output}")
        return False

def install_python_deps():
    """Устанавливает Python зависимости"""
    print("\n📦 Установка Python зависимостей...")
    
    success, output = run_command([
        sys.executable, "-m", "pip", "install", "-r", "requirements.txt"
    ])
    
    if success:
        print("✅ Python зависимости установлены")
        return True
    else:
        print(f"❌ Ошибка установки: {output}")
        return False

def check_gh_cli():
    """Проверяет GitHub CLI и аутентификацию"""
    print("\n🔍 Проверка GitHub CLI...")
    
    # Проверка установки
    success, output = run_command(["gh", "--version"])
    if not success:
        print("❌ GitHub CLI не установлен")
        print("📥 Установите: https://cli.github.com/")
        return False
    
    print(f"✅ GitHub CLI установлен: {output.split()[2]}")
    
    # Проверка аутентификации
    success, output = run_command(["gh", "auth", "status"])
    if success:
        print("✅ GitHub CLI аутентифицирован")
        return True
    else:
        print("⚠️ Требуется аутентификация")
        print("Выполните: gh auth login --web")
        return False

def create_env_template():
    """Создаёт шаблон .env файла"""
    env_file = ".env"
    
    if os.path.exists(env_file):
        print(f"\n✅ {env_file} уже существует")
        return
    
    template = """# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_bot_token_here
CHANNEL_ID=@your_channel_id

# OpenAI API Key
OPENAI_API_KEY=your_openai_key_here

# GitHub Token (для Copilot SDK в локальной разработке)
GITHUB_TOKEN=your_github_token_here

# Cache Directory
CACHE_DIR=cache
"""
    
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(template)
    
    print(f"\n✅ Создан шаблон {env_file}")
    print("⚠️ Заполните файл перед запуском!")

def create_gitignore():
    """Создаёт .gitignore"""
    gitignore_file = ".gitignore"
    
    if os.path.exists(gitignore_file):
        return
    
    content = """# Environment
.env
*.env

# Cache
cache/
*.json

# Python
__pycache__/
*.py[cod]
*$py.class
venv/
env/

# Images
*.jpg
*.jpeg
*.png

# IDE
.vscode/
.idea/

# Logs
*.log
"""
    
    with open(gitignore_file, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"✅ Создан {gitignore_file}")

def create_directories():
    """Создаёт необходимые директории"""
    dirs = ["cache", "scripts"]
    
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    
    print(f"✅ Проверены директории: {', '.join(dirs)}")

def main():
    print("=" * 60)
    print("🚀 KIBER SOS Bot - Автоматическая установка")
    print("=" * 60)
    print()
    
    # Проверки
    check_python_version()
    
    has_node = check_node()
    has_gh_cli = check_gh_cli()
    
    # Установка
    if has_node:
        install_copilot_cli()
    
    install_python_deps()
    
    # Создание файлов
    create_directories()
    create_env_template()
    create_gitignore()
    
    # Итоги
    print("\n" + "=" * 60)
    print("✅ Установка завершена!")
    print("=" * 60)
    
    print("\n📋 Следующие шаги:")
    print("1. Заполните .env файл своими токенами")
    
    if not has_gh_cli:
        print("2. Установите GitHub CLI: https://cli.github.com/")
        print("3. Аутентифицируйтесь: gh auth login --web")
    elif not check_gh_cli():
        print("2. Аутентифицируйтесь: gh auth login --web")
    
    print(f"3. Запустите бота: python scripts/kibersos_autopost.py")
    print()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Установка прервана пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)
