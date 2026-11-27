import os
import warnings
from google.colab import userdata

warnings.filterwarnings("ignore", category=FutureWarning)

secret_keys = [
    "ANTHROPIC_API_KEY",
    "BINANCE_API_KEY",
    "BINANCE_SECRET",
    "BLOCKWORKS_API_KEY",
    "CMC_API_KEY",
    "FRED_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TWELVEDATA_API_KEY",
]

for key in secret_keys:
    try:
        val = userdata.get(key)
        if val:
            os.environ[key] = str(val).strip()
    except Exception:
        pass

%cd /content
!rm -rf quant-AgentLang-247
!git clone https://github.com/lang-fr/quant-AgentLang-247.git
%cd quant-AgentLang-247
!pip install -q -r requirements.txt
!python -W ignore::FutureWarning main.py
