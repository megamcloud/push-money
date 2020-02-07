from decimal import Decimal

import requests
from mintersdk.sdk.wallet import MinterWallet

from api.models import PushWallet
from config import BIP2PHONE_API_KEY
from providers.mscan import MscanAPI
from minter.tx import send_coin_tx
from minter.utils import to_bip

# BIP2PHONE_API_URL = 'https://biptophone.ru/api.php'
# requests to my proxy, because my server doesn't see API host :)
BIP2PHONE_API_URL = 'https://static.255.135.203.116.clients.your-server.de/api.php'
BIP2PHONE_PAYMENT_ADDRESS = 'Mx403b763ab039134459448ca7875c548cd5e80f77'


def mobile_top_up(wallet: PushWallet, phone=None, amount=None):
    amount = float(amount)

    phone_reqs = get_tx_requirements(phone)
    if not phone_reqs:
        return f'Phone number {phone} not supported or invalid'

    response = MscanAPI.get_balance(wallet.address)
    balance = response['balance']
    nonce = int(response['transaction_count']) + 1
    to_send = amount or to_bip(balance['BIP'])

    private_key = MinterWallet.create(mnemonic=wallet.mnemonic)['private_key']

    tx = send_coin_tx(
        private_key, 'BIP', to_send, BIP2PHONE_PAYMENT_ADDRESS, nonce,
        payload=phone_reqs['payload'])
    fee = float(to_bip(tx.get_fee()))
    min_topup = float(phone_reqs['min_bip_value']) + fee
    if to_send < min_topup:
        return f"Minimal top-up: {min_topup} BIP"

    tx = send_coin_tx(
        private_key, 'BIP', to_send - fee, BIP2PHONE_PAYMENT_ADDRESS, nonce,
        payload=phone_reqs['payload'])
    MscanAPI.send_tx(tx, wait=True)
    return True


def mobile_validate_normalize(phone):
    r = requests.post(BIP2PHONE_API_URL, data={'key1': BIP2PHONE_API_KEY, 'phone': phone, 'validation': 1})
    r.raise_for_status()
    data = r.json()
    return {'valid': bool(int(data['isvalid'])), 'phone': data['phone']}


def get_tx_requirements(phone):
    r = requests.post(BIP2PHONE_API_URL, data={'key1': BIP2PHONE_API_KEY, 'phone': phone, 'contact': 1})
    r.raise_for_status()
    data = r.json()
    if 'keyword' not in data:
        return None
    return {
        'payload': data['keyword'],
        'country': data['country'],
        'min_bip_value': Decimal(str(data['minbip']))
    }


def get_last_payment_status(phone):
    r = requests.post(BIP2PHONE_API_URL, data={'key1': BIP2PHONE_API_KEY, 'phone': phone, 'status': 1})
    r.raise_for_status()
    data = r.json()
    if 'success' not in data:
        return None
    return {
        'status': int(data['success']),
        'bip': Decimal(data.get('bip')),
        'rub': Decimal(data.get('amount')),
    }


def get_info():
    r = requests.post(BIP2PHONE_API_URL, data={'key1': BIP2PHONE_API_KEY, 'curs': 1})
    r.raise_for_status()
    data = r.json()
    return {'RUB': float(data['RUB']), 'limit_bip': float(data['LIMIT'])}
