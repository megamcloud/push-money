from datetime import datetime
from http import HTTPStatus
from flask import Blueprint, jsonify, request, url_for
from mintersdk.sdk.wallet import MinterWallet
from mintersdk.shortcuts import to_bip
from api.logic.core import generate_and_save_wallet, get_address_balance, spend_balance, \
    get_spend_list
from api.models import PushWallet, PushCampaign, Recipient, CustomizationSetting
from minter.helpers import TxDeeplink
from minter.tx import send_coin_tx, estimate_custom_fee
from providers.currency_rates import bip_to_usdt, fiat_to_usd_rates
from providers.explorer import get_custom_coin_symbols
from providers.minter import send_coins
from providers.nodeapi import NodeAPI

bp_api = Blueprint('api', __name__, url_prefix='/api')


@bp_api.route('', methods=['GET'])
def health():
    return f'Api ok. <a href="{url_for("swagger.swag")}">Swagger</a>'


@bp_api.route('/custom-coins')
def custom_coins():
    return jsonify({"symbols": get_custom_coin_symbols()})


@bp_api.route('/exchange-rates')
def exchange_rates():
    bip_usd_price = bip_to_usdt(1)
    fiat_usd = fiat_to_usd_rates()
    x2bip = {'BIP': 1.0, **{
        currency: 1 / (usd_value * bip_usd_price)
        for currency, usd_value in fiat_usd.items() if currency in ['USD', 'UAH', 'EUR', 'RUB']
    }}
    return jsonify(x2bip)


@bp_api.route('/deeplink')
def deeplink():
    address = request.args.get('address')
    amount = request.args.get('amount')
    coin = request.args.get('coin')
    nofee = 'nofee' in request.args

    if not address:
        return jsonify({'success': False, 'error': '"address" key is required'}), HTTPStatus.BAD_REQUEST
    if not amount:
        return jsonify({'success': False, 'error': '"amount" key is required'}), HTTPStatus.BAD_REQUEST
    if not coin:
        return jsonify({'success': False, 'error': '"coin" key is required'}), HTTPStatus.BAD_REQUEST

    address = address.strip()
    fee = 0
    if not nofee:
        fee = float(estimate_custom_fee(coin) or 0)
    amount = float(amount) + fee
    coin = coin.strip().upper()
    link = TxDeeplink.create('send', to=address, value=amount, coin=coin, data_only=False)
    return {
        'success': True,
        'web': link.web,
        'mobile': link.mobile
    }


@bp_api.route('/push/create', methods=['POST'])
def push_create():
    """
    swagger: swagger/core/push-create.yml
    """
    payload = request.get_json() or {}
    sender, recipient = payload.get('sender'), payload.get('recipient')
    password = payload.get('password')
    amount = payload.get('amount')
    coin = payload.get('coin', 'BIP')

    customization_setting_id = payload.get('customization_setting_id')
    setting = CustomizationSetting.get_or_none(id=customization_setting_id)
    if not setting:
        jsonify({'error': 'Customization setting does not exist'}), HTTPStatus.BAD_REQUEST

    wallet = generate_and_save_wallet(
        sender=sender, recipient=recipient, password=password,
        customization_setting_id=customization_setting_id)
    response = {
        'address': wallet.address,
        'link_id': wallet.link_id
    }
    if amount:
        w = MinterWallet.create()
        tx = send_coin_tx(w['private_key'], coin, float(amount), w['address'], 1, gas_coin=coin)
        tx_fee = float(to_bip(NodeAPI.estimate_tx_comission(tx.signed_tx)['commission']))
        response['deeplink'] = TxDeeplink.create('send', to=wallet.address, value=float(amount) + tx_fee, coin=coin).mobile
    return jsonify(response)


@bp_api.route('/push/<link_id>/info', methods=['GET'])
def push_info(link_id):
    """
    swagger: swagger/core/push-info.yml
    """
    wallet = PushWallet.get_or_none(link_id=link_id)
    if not wallet:
        return jsonify({'error': 'Link does not exist'}), HTTPStatus.NOT_FOUND

    return jsonify({
        'seen': wallet.seen,
        'sender': wallet.sender,
        'recipient': wallet.recipient,
        'is_protected': wallet.password_hash is not None,
        'customization_id': wallet.customization_setting_id,
    })


@bp_api.route('/push/<link_id>/mnemonic', methods=['GET', 'POST'])
def get_mnemonic(link_id):
    payload = request.get_json() or {}
    password = payload.get('password')

    wallet = PushWallet.get_or_none(link_id=link_id)
    if not wallet:
        return jsonify({'error': 'Link does not exist'}), HTTPStatus.NOT_FOUND

    if not wallet.auth(password):
        return jsonify({'error': 'Incorrect password'}), HTTPStatus.UNAUTHORIZED

    return jsonify({'mnemonic': wallet.mnemonic})


@bp_api.route('/push/<link_id>/balance', methods=['POST', 'GET'])
def push_balance(link_id):
    """
    swagger: swagger/core/push-balance.yml
    """
    payload = request.get_json() or {}
    password = payload.get('password')

    wallet = PushWallet.get_or_none(link_id=link_id)
    if not wallet:
        return jsonify({'error': 'Link does not exist'}), HTTPStatus.NOT_FOUND

    if not wallet.auth(password):
        return jsonify({'error': 'Incorrect password'}), HTTPStatus.UNAUTHORIZED

    # зарефакторить
    virtual_balance = None if wallet.virtual_balance == '0' else wallet.virtual_balance
    if virtual_balance is not None and not wallet.seen:
        if wallet.sent_from:
            from_w = PushWallet.get(link_id=wallet.sent_from)
            result = send_coins(from_w, wallet.address, amount=to_bip(wallet.virtual_balance), wait=False)
            if result is not True:
                return jsonify({'error': result}), HTTPStatus.INTERNAL_SERVER_ERROR
            wallet.virtual_balance = '0'
            wallet.save()
        else:
            cmp = PushCampaign.get_or_none(id=wallet.campaign_id)
            cmp_wallet = PushWallet.get(link_id=cmp.wallet_link_id)
            result = send_coins(cmp_wallet, wallet.address, amount=to_bip(wallet.virtual_balance), wait=False)
            if result is not True:
                return jsonify({'error': result}), HTTPStatus.INTERNAL_SERVER_ERROR
            wallet.virtual_balance = '0'
            recipient = Recipient.get(wallet_link_id=wallet.link_id)
            recipient.linked_at = datetime.utcnow()
            recipient.save()
            wallet.save()

    if not wallet.seen:
        wallet.seen = True
        wallet.save()
    balance = get_address_balance(wallet.address, virtual=virtual_balance)
    response = {
        'address': wallet.address,
        **balance
    }
    return jsonify(response)


@bp_api.route('/spend/list', methods=['GET'])
def spend_options():
    """
    swagger: swagger/core/spend-list.yml
    """
    categories = get_spend_list()
    return jsonify(categories)


@bp_api.route('/spend/<link_id>', methods=['POST'])
def make_spend(link_id):
    """
    swagger: swagger/core/spend-make.yml
    """
    payload = request.get_json() or {}
    password = payload.get('password')

    wallet = PushWallet.get_or_none(link_id=link_id)
    if not wallet:
        return jsonify({'success': False, 'error': 'Link does not exist'}), HTTPStatus.NOT_FOUND

    if 'slug' not in payload and 'option' not in payload:
        return jsonify({'success': False, 'error': '"slug" key is required'}), HTTPStatus.BAD_REQUEST

    new_password = None
    slug = payload.get('slug', payload.get('option'))
    if password and slug == 'resend':
        new_password = password
    if not wallet.auth(password):
        return jsonify({'success': False, 'error': 'Incorrect password'}), HTTPStatus.UNAUTHORIZED

    confirm = bool(int(request.args.get('confirm', 1)))
    params = payload.get('params', {})
    if params.get('amount'):
        params['amount'] = float(params['amount'])

    if slug == 'resend':
        params['new_password'] = new_password

    result = spend_balance(wallet, slug, confirm=confirm, **params)
    if isinstance(result, str):
        return jsonify({'success': False, 'error': result}), HTTPStatus.INTERNAL_SERVER_ERROR

    result = {} if isinstance(result, bool) else result
    return jsonify({'success': True, **result})
