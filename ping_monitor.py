import subprocess
import time
import requests
import json
import logging
import os
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Dict, Any
import threading
import argparse
from pathlib import Path

# Configuração de logging
log_level = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/app/logs/ping_monitor.log'),
        logging.StreamHandler()
    ]
)

@dataclass
class AlertConfig:
    """Configurações de alerta"""
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    webhook_url: Optional[str] = None
    whatsapp_api_url: Optional[str] = None
    whatsapp_token: Optional[str] = None
    whatsapp_instance: Optional[str] = None
    whatsapp_number: Optional[str] = None

class PingMonitor:
    def __init__(self, host: str, alert_config: AlertConfig, config: Dict[str, Any] = None):
        self.host = host
        self.alert_config = alert_config
        self.config = config or {}
        self.is_running = False
        self.consecutive_failures = 0
        self.last_status = None
        self.total_pings = 0
        self.failed_pings = 0
        self.max_failures_before_alert = int(os.getenv('MAX_FAILURES', '3'))
        self.ping_interval = int(os.getenv('PING_INTERVAL', '1'))
        self.log_status_interval = 60
        
    def ping_host(self) -> bool:
        """Executa ping para o host"""
        try:
            if os.name == 'nt':  # Windows
                result = subprocess.run(
                    ['ping', '-n', '1', '-w', '1000', self.host],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            else:  # Linux/Mac
                result = subprocess.run(
                    ['ping', '-c', '1', '-W', '1', self.host],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            
            return result.returncode == 0
            
        except subprocess.TimeoutExpired:
            logging.warning(f"Timeout no ping para {self.host}")
            return False
        except Exception as e:
            logging.error(f"Erro ao fazer ping: {e}")
            return False
    
    def check_evolution_api_status(self) -> bool:
        """Verifica se a Evolution API está funcionando"""
        if not self.alert_config.whatsapp_api_url:
            return False
            
        try:
            url = f"{self.alert_config.whatsapp_api_url}/instance/connectionState/{self.alert_config.whatsapp_instance}"
            headers = {'apikey': self.alert_config.whatsapp_token}
            
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                return data.get('instance', {}).get('state') == 'open'
            return False
        except Exception as e:
            logging.warning(f"Erro ao verificar Evolution API: {e}")
            return False
    
    def send_telegram_alert(self, message: str):
        """Envia alerta via Telegram"""
        if not self.alert_config.telegram_bot_token or not self.alert_config.telegram_chat_id:
            return
            
        try:
            url = f"https://api.telegram.org/bot{self.alert_config.telegram_bot_token}/sendMessage"
            data = {
                'chat_id': self.alert_config.telegram_chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                logging.info("Alerta enviado via Telegram")
            else:
                logging.error(f"Erro ao enviar Telegram: {response.status_code}")
                
        except Exception as e:
            logging.error(f"Erro ao enviar alerta Telegram: {e}")
    
    def send_webhook_alert(self, message: str, status: str):
        """Envia alerta via webhook"""
        if not self.alert_config.webhook_url:
            return
            
        try:
            payload = {
                'timestamp': datetime.now().isoformat(),
                'host': self.host,
                'status': status,
                'message': message,
                'consecutive_failures': self.consecutive_failures,
                'total_pings': self.total_pings,
                'failed_pings': self.failed_pings,
                'success_rate': ((self.total_pings - self.failed_pings) / self.total_pings * 100) if self.total_pings > 0 else 0,
                'container_id': os.getenv('HOSTNAME', 'unknown'),
                'environment': os.getenv('NODE_ENV', 'production')
            }
            
            response = requests.post(
                self.alert_config.webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            
            if response.status_code == 200:
                logging.info("Alerta enviado via webhook")
            else:
                logging.error(f"Erro ao enviar webhook: {response.status_code}")
                
        except Exception as e:
            logging.error(f"Erro ao enviar webhook: {e}")
    
    def send_whatsapp_alert(self, message: str):
        """Envia alerta via WhatsApp usando Evolution API"""
        if not self.alert_config.whatsapp_api_url or not self.alert_config.whatsapp_token:
            return
        
        # Verificar se a API está funcionando
        if not self.check_evolution_api_status():
            logging.error("Evolution API não está conectada")
            return
            
        try:
            headers = {
                'apikey': self.alert_config.whatsapp_token,
                'Content-Type': 'application/json'
            }
            
            payload = {
                'number': self.alert_config.whatsapp_number,
                'text': message
            }
            
            url = f"{self.alert_config.whatsapp_api_url}/message/sendText/{self.alert_config.whatsapp_instance}"
            
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=10
            )
            
            if response.status_code in [200, 201]:
                logging.info("Alerta enviado via WhatsApp (Evolution API)")
            else:
                logging.error(f"Erro ao enviar WhatsApp: {response.status_code} - {response.text}")
                
        except Exception as e:
            logging.error(f"Erro ao enviar WhatsApp: {e}")
    
    def send_alerts(self, message: str, status: str):
        """Envia alertas para todos os canais configurados"""
        # Usar threads para não bloquear o monitoramento
        if self.alert_config.telegram_bot_token:
            threading.Thread(target=self.send_telegram_alert, args=(message,)).start()
        
        if self.alert_config.webhook_url:
            threading.Thread(target=self.send_webhook_alert, args=(message, status)).start()
        
        if self.alert_config.whatsapp_token:
            threading.Thread(target=self.send_whatsapp_alert, args=(message,)).start()
    
    def format_alert_message(self, status: str) -> str:
        """Formata mensagem de alerta"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        container_id = os.getenv('HOSTNAME', 'unknown')
        
        if status == "DOWN":
            return f"""
🔴 *ALERTA - HOST INATIVO*
🖥️ Host: {self.host}
📦 Container: {container_id}
⏰ Timestamp: {timestamp}
❌ Falhas consecutivas: {self.consecutive_failures}
📊 Taxa de sucesso: {((self.total_pings - self.failed_pings) / self.total_pings * 100):.1f}%
🔄 Total de pings: {self.total_pings}
            """.strip()
        else:
            return f"""
✅ *RECUPERAÇÃO - HOST ATIVO*
🖥️ Host: {self.host}
📦 Container: {container_id}
⏰ Timestamp: {timestamp}
📊 Taxa de sucesso: {((self.total_pings - self.failed_pings) / self.total_pings * 100):.1f}%
🔄 Total de pings: {self.total_pings}
            """.strip()
    
    def start_monitoring(self):
        """Inicia o monitoramento"""
        self.is_running = True
        container_id = os.getenv('HOSTNAME', 'unknown')
        logging.info(f"Iniciando monitoramento do host: {self.host} no container: {container_id}")
        logging.info(f"Configurações: Intervalo={self.ping_interval}s, Max falhas={self.max_failures_before_alert}")
        
        while self.is_running:
            try:
                self.total_pings += 1
                ping_success = self.ping_host()
                
                if ping_success:
                    if self.last_status == "DOWN":
                        message = self.format_alert_message("UP")
                        self.send_alerts(message, "UP")
                        logging.info(f"Host {self.host} recuperado!")
                    
                    self.consecutive_failures = 0
                    self.last_status = "UP"
                    
                else:
                    self.failed_pings += 1
                    self.consecutive_failures += 1
                    
                    if self.consecutive_failures >= self.max_failures_before_alert and self.last_status != "DOWN":
                        message = self.format_alert_message("DOWN")
                        self.send_alerts(message, "DOWN")
                        logging.error(f"Host {self.host} inativo após {self.consecutive_failures} tentativas!")
                        self.last_status = "DOWN"
                
                # Log de status periódico
                if self.total_pings % self.log_status_interval == 0:
                    success_rate = ((self.total_pings - self.failed_pings) / self.total_pings * 100)
                    logging.info(f"Status: {self.host} - Taxa de sucesso: {success_rate:.1f}% ({self.total_pings} pings)")
                
                time.sleep(self.ping_interval)
                
            except KeyboardInterrupt:
                logging.info("Monitoramento interrompido pelo usuário")
                break
            except Exception as e:
                logging.error(f"Erro durante monitoramento: {e}")
                time.sleep(5)
    
    def stop_monitoring(self):
        """Para o monitoramento"""
        self.is_running = False
        logging.info("Monitoramento finalizado")

def load_config_from_env() -> AlertConfig:
    """Carrega configuração das variáveis de ambiente"""
    return AlertConfig(
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN'),
        telegram_chat_id=os.getenv('TELEGRAM_CHAT_ID'),
        webhook_url=os.getenv('WEBHOOK_URL'),
        whatsapp_api_url=os.getenv('WHATSAPP_API_URL', 'http://evolution-api:8080'),
        whatsapp_token=os.getenv('WHATSAPP_API_KEY'),
        whatsapp_instance=os.getenv('WHATSAPP_INSTANCE', 'ping-monitor'),
        whatsapp_number=os.getenv('WHATSAPP_NUMBER')
    )

def health_check():
    """Endpoint de health check para o EasyPanel"""
    from flask import Flask, jsonify
    
    app = Flask(__name__)
    
    @app.route('/health')
    def health():
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'container_id': os.getenv('HOSTNAME', 'unknown')
        })
    
    return app

def main():
    parser = argparse.ArgumentParser(description='Monitor de Ping com Alertas')
    parser.add_argument('--host', help='Host para monitorar')
    parser.add_argument('--health-server', action='store_true', help='Iniciar servidor de health check')
    args = parser.parse_args()
    
    # Criar diretório de logs se não existir
    Path('/app/logs').mkdir(parents=True, exist_ok=True)
    
    # Host para monitorar
    host = args.host or os.getenv('MONITOR_HOST', '8.8.8.8')
    
    # Carregar configuração das variáveis de ambiente
    alert_config = load_config_from_env()
    
    # Mostrar configurações ativas
    active_alerts = []
    if alert_config.telegram_bot_token:
        active_alerts.append('Telegram')
    if alert_config.webhook_url:
        active_alerts.append('Webhook')
    if alert_config.whatsapp_token:
        active_alerts.append('WhatsApp')
    
    logging.info(f"Alertas ativos: {', '.join(active_alerts) if active_alerts else 'Nenhum'}")
    logging.info(f"Container ID: {os.getenv('HOSTNAME', 'unknown')}")
    
    # Iniciar servidor de health check se solicitado
    if args.health_server:
        app = health_check()
        app.run(host='0.0.0.0', port=8000)
        return
    
    # Criar e iniciar monitor
    monitor = PingMonitor(host, alert_config)
    
    try:
        monitor.start_monitoring()
    except KeyboardInterrupt:
        monitor.stop_monitoring()
        logging.info("Monitoramento finalizado.")

if __name__ == "__main__":
    main()
