import os
import sys
import subprocess
import webbrowser
import threading
import time

def main():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(root_dir, "backend")
    venv_python = os.path.join(backend_dir, ".venv", "Scripts", "python.exe")
    
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    print("\n" + "="*60)
    print(" Launching WhatsApp Order Status Bot")
    print(" Single URL for Backend & Frontend: http://localhost:8000/")
    print(" Sign in with the ADMIN_KEY value from backend/.env")
    print("="*60 + "\n")

    def open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:8000/")

    threading.Thread(target=open_browser, daemon=True).start()

    cmd = [venv_python, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
    try:
        subprocess.run(cmd, cwd=backend_dir)
    except KeyboardInterrupt:
        print("\nStopping server...")

if __name__ == "__main__":
    main()
