import logging  # Будем вести лог
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # ВременнАя зона
from typing import Any  # Любой тип
from uuid import uuid4  # Номера подписок должны быть уникальными во времени и пространстве
from json import loads, JSONDecodeError, dumps  # Сервер WebSockets работает с JSON сообщениями
from threading import Thread  # Подписки сервера WebSockets будем получать в отдельном потоке

import keyring  # Безопасное хранение торгового токена
import keyring.errors  # Ошибки хранилища
from requests import post, get, Response  # Запросы/ответы через HTTP API
from urllib3.exceptions import SSLError  # Ошибка SSL
from websockets.sync.client import connect, ClientConnection  # Подключение к серверу WebSockets в синхронном режиме
from websockets.exceptions import ConnectionClosed  # Событие закрытия соединения сервера WebSockets


class BCSPy:
    """Работа с БКС торговое API https://trade-api.bcs.ru из Python"""
    tz_msk = ZoneInfo('Europe/Moscow')  # Время UTC будем приводить к московскому времени
    http_server = 'https://be.broker.ru'  # Сервер запросов
    ws_server = 'wss://ws.broker.ru'  # Сервер подписок WebSocket
    logger = logging.getLogger('BCSPy')  # Будем вести лог

    def __init__(self, refresh_token=None):
        """Инициализация

        :param str refresh_token: Токен
        """
        # Соединения WebSockets
        self.ws_limits: ClientConnection | None = None  # Лимиты
        self.ws_portfolio: ClientConnection | None = None  # Портфель
        self.ws_executions: ClientConnection | None = None  # Исполненные заявки
        self.ws_transactions: ClientConnection | None = None  # Обновления о статусе созданных заявок
        self.ws_quotes: ClientConnection | None = None  # Котировки
        self.ws_last_candle: ClientConnection | None = None  # Последние свечи
        self.ws_order_book: ClientConnection | None = None  # Стакан
        self.ws_trades: ClientConnection | None = None  # Обезличенные сделки
        self.ws_margins: ClientConnection | None = None  # Маржинальные показатели портфеля

        # События
        self.on_limit = Event()  # Лимиты
        self.on_portfolio = Event()  # Портфель
        self.on_execution = Event()  # Исполненные заявки
        self.on_transaction = Event()  # Обновления о статусе созданных заявок
        self.on_quote = Event()  # Котировки
        self.on_candle = Event()  # Последние свечи
        self.on_order_book = Event()  # Стакан
        self.on_trade = Event()  # Обезличенные сделки
        self.on_margin = Event()  # Маржинальные показатели портфеля

        if refresh_token is None:  # Если токен не указан
            self.refresh_token = self.get_long_token_from_keyring('BCSPy', 'refresh_token')  # то получаем его из защищенного хранилища по частям
            if self.refresh_token is None:  # Если токен не найден
                self.logger.fatal('Токен не найден в системном хранилище. Вызовите bp_provider = BCSPy(''<Токен>'')')
        else:  # Если указан токен
            self.refresh_token = refresh_token  # то запоминаем токен
            self.set_long_token_to_keyring('BCSPy', 'refresh_token', self.refresh_token)  # Сохраняем его в защищенное хранилище

        self.access_token = None  # Токен доступа
        self.access_token_expired = 0  # UNIX время в секундах окончания срока действия токена доступа

    def __enter__(self):
        """Вход в класс, например, с with"""
        return self

    # Авторизация

    def get_access_token(self) -> str | None:  # https://trade-api.bcs.ru/authorization
        """Получение токена доступа
        :return: Токен доступа
        """
        now = int(datetime.timestamp(datetime.now()))  # Текущая дата и время в виде UNIX времени в секундах
        if self.access_token is None or now >= self.access_token_expired:  # Если токен доступа не был выдан или был просрочен
            try:
                response = post(url=f'{self.http_server}/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token',  # Запрашиваем новый токен доступа
                                data={'client_id': 'trade-api-write',  # Токен для торговли
                                      'grant_type': 'refresh_token',  # Токен доступа будет получать через токен
                                      'refresh_token': self.refresh_token})  # Токен
            except SSLError:  # Ошибка соединения SSL
                self.logger.error('Ошибка соединения SSL')  # Событие ошибки
                self.access_token = None  # Сбрасываем токен доступа
                self.access_token_expired = 0  # Сбрасываем время окончания срока действия токена доступа
                return None
            if response.status_code != 200:  # Если при получении токена возникла ошибка
                self.logger.error(f'Ошибка получения JWT токена: {response.status_code}')  # Событие ошибки
                self.access_token = None  # Сбрасываем токен доступа
                self.access_token_expired = 0  # Сбрасываем время окончания срока действия токена доступа
                return None
            # Токен получен
            access_token = response.json()  # Читаем данные JSON
            self.access_token = access_token['access_token']  # Получаем токен доступа
            self.access_token_expired = now + int(access_token['expires_in'])  # Получаем время окончания срока действия токена доступа. Ставим его на 5 секунд раньше, чтобы исключить просрочку
        return self.access_token

    # Получение информации о вашем портфеле через сервис «Лимиты» https://trade-api.bcs.ru/limits

    def get_limits(self):
        """Получение информации о портфеле через Лимиты"""
        return self._check_result(get(url=f'{self.http_server}/trade-api-bff-limit/api/v1/limits', headers=self._get_headers()))

    def subscribe_limits(self):
        """Подписка на Лимиты"""
        if self.ws_limits is None:  # Если не подписаны
            self.logger.debug('Подписка на Лимиты')
            self.ws_limits = connect(uri=f'{self.ws_server}/trade-api-bff-limit/api/v1/limits/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Limits', self.ws_limits, self.on_limit), name='LimitsThread').start()  # Создаем и запускаем поток

    def unsubscribe_limits(self):
        """Отмена подписки на Лимиты"""
        if self.ws_limits is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на Лимиты')
            self.ws_limits.close()  # то закрваем соединение
            self.ws_limits = None  # Сбрасываем подключение

    # Получение информации о вашем портфеле через сервис «Портфель» https://trade-api.bcs.ru/portfolio

    def get_portfolio(self):
        """Получение информации о вашем портфеле через сервис «Портфель»"""
        return self._check_result(get(url=f'{self.http_server}/trade-api-bff-portfolio/api/v1/portfolio', headers=self._get_headers()))

    def subscribe_portfolio(self):
        """Подписка на Портфель"""
        if self.ws_portfolio is None:  # Если не подписаны
            self.logger.debug('Подписка на Портфель')
            self.ws_portfolio = connect(uri=f'{self.ws_server}/trade-api-bff-portfolio/api/v1/portfolio/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Portfolio', self.ws_portfolio, self.on_portfolio), name='PortfolioThread').start()  # Создаем и запускаем поток

    def unsubscribe_portfolio(self):
        """Отмена подписки на Портфель"""
        if self.ws_portfolio is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на Портфель')
            self.ws_portfolio.close()  # то закрваем соединение
            self.ws_portfolio = None  # Сбрасываем подключение

    # Заявки https://trade-api.bcs.ru/operations

    def create_order(self, side: int, order_type: int, order_quantity: int, ticker: str, class_code: str, price: float = None, client_order_id: str = str(uuid4())):  # https://trade-api.bcs.ru/operations/create
        """Создание торговой заявки

        :param int side: Сторона заявки (1 - покупка, 2 - продажа)
        :param int order_type: Тип заявки (1 - рыночная, 2 - лимитная)
        :param int order_quantity: Количество в заявке (шт.), > 0
        :param str ticker: Тикер
        :param str class_code: Режим торгов
        :param float price: Цена для лимитной заявки (> 0). Допустимо 8 знаков после запятой
        :param str client_order_id: Идентификатор заявки
        """
        params = {'clientOrderId': client_order_id, 'side': side, 'orderType': order_type, 'orderQuantity': order_quantity, 'ticker': ticker, 'classCode': class_code}
        if order_type == 2:  # Для лимитной заявки
            params['price'] = price  # указываем цену
        return self._check_result(post(url=f'{self.http_server}/trade-api-bff-operations/api/v1/orders', json=params, headers=self._get_headers()))

    def cancel_order(self, original_client_order_id: str, client_order_id: str = str(uuid4())):  # https://trade-api.bcs.ru/operations/cancel
        """Отмена заявки

        :param str original_client_order_id: Идентификатор заменяемой заявки. Идентификатор запроса на выставление заявки (id исходного запроса)
        :param str client_order_id: Новый идентификатор для отмены
        """
        params = {'clientOrderId': client_order_id}
        return self._check_result(post(url=f'{self.http_server}/trade-api-bff-operations/api/v1/orders/{original_client_order_id}/cancel', json=params, headers=self._get_headers()))

    def edit_order(self, original_client_order_id: str, price: float, order_quantity: int, class_code: str, client_order_id: str = str(uuid4())):  # https://trade-api.bcs.ru/operations/edit
        """Изменение заявки

        :param str original_client_order_id: Идентификатор заменяемой заявки
        :param float price: Цена (> 0). Допустимо 8 знаков после запятой
        :param int order_quantity: Количество в заявке (шт.), > 0
        :param str class_code: Режим торгов
        :param str client_order_id: Идентификатор новой заявки
        """
        params = {'clientOrderId': client_order_id, 'price': price, 'orderQuantity': order_quantity, 'classCode': class_code}
        return self._check_result(post(url=f'{self.http_server}/trade-api-bff-operations/api/v1/orders/{original_client_order_id}', json=params, headers=self._get_headers()))

    def get_order(self, original_client_order_id: str):  # https://trade-api.bcs.ru/operations/status
        """Получение статуса заявки

        :param str original_client_order_id: Идентификатор заявки
        """
        return self._check_result(get(url=f'{self.http_server}/trade-api-bff-operations/api/v1/orders/{original_client_order_id}', headers=self._get_headers()))

    # Получение информации об исполненных заявках https://trade-api.bcs.ru/operations/execution

    def subscribe_executions(self):
        """Подписка на исполненные заявки"""
        if self.ws_executions is None:  # Если не подписаны
            self.logger.debug('Подписка на исполненные заявки')
            self.ws_executions = connect(uri=f'{self.ws_server}/trade-api-bff-operations/api/v1/orders/execution/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Executions', self.ws_executions, self.on_execution), name='ExecutionsThread').start()  # Создаем и запускаем поток

    def unsubscribe_executions(self):
        """Отмена подписки на исполненные заявки"""
        if self.ws_executions is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на исполненные заявки')
            self.ws_executions.close()  # то закрваем соединение
            self.ws_executions = None  # Сбрасываем подключение

    # Получение обновлений о статусе созданных заявок https://trade-api.bcs.ru/operations/transaction-status

    def subscribe_transactions(self):
        """Подписка на обновления о статусе созданных заявок"""
        if self.ws_transactions is None:  # Если не подписаны
            self.logger.debug('Подписка на обновления о статусе созданных заявок')
            self.ws_transactions = connect(uri=f'{self.ws_server}/trade-api-bff-operations/api/v1/orders/transaction/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Transactions', self.ws_transactions, self.on_transaction), name='TransactionsThread').start()  # Создаем и запускаем поток

    def unsubscribe_transactions(self):
        """Отмена подписки на обновления о статусе созданных заявок"""
        if self.ws_transactions is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на обновления о статусе созданных заявок')
            self.ws_transactions.close()  # то закрваем соединение
            self.ws_transactions = None  # Сбрасываем подключение

    # Рыночные данные https://trade-api.bcs.ru/market-data

    # Котировки https://trade-api.bcs.ru/market-data/quotes

    def subscribe_quotes(self, subscribe_type: int, instruments: list[dict[str, str]]):
        """Подписка на котировки

        :param int subscribe_type: 0 - подписка, 1 - отмена подписки
        :param list[dict[str, str]] instruments: Список инструментов. instruments = [{'classCode': 'TQBR', 'ticker': 'SBER'}, {'classCode': 'SPBFUT', 'ticker': 'CNYRUBF'}]
        """
        if self.ws_quotes is None:  # Если не подписаны
            self.ws_quotes = connect(uri=f'{self.ws_server}/trade-api-market-data-connector/api/v1/market-data/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Quotes', self.ws_quotes, self.on_quote), name='QuotesThread').start()  # Создаем и запускаем поток
        request = {'subscribeType': subscribe_type, 'dataType': 3, 'instruments': instruments}  # Формируем запрос
        self.logger.debug(f'Подписка на котировки: {request}')
        self.ws_quotes.send(dumps(request))  # Переводим JSON в строку, отправляем запрос

    def unsubscribe_quotes(self):
        """Отмена подписки на котировки"""
        if self.ws_quotes is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на котировки')
            self.ws_quotes.close()  # то закрваем соединение
            self.ws_quotes = None  # Сбрасываем подключение

    # Последняя свеча https://trade-api.bcs.ru/market-data/last-candle

    def subscribe_last_candles(self, subscribe_type: int, instruments: list[dict[str, str]], time_frame: str):
        """Подписка на последние свечи

        :param int subscribe_type: 0 - подписка, 1 - отмена подписки
        :param list[dict[str, str]] instruments: Список инструментов. instruments = [{'classCode': 'TQBR', 'ticker': 'SBER'}, {'classCode': 'SPBFUT', 'ticker': 'CNYRUBF'}]
        :param str time_frame: Временной интервал (M1, M2, ... M60)
        """
        if self.ws_last_candle is None:  # Если не подписаны
            self.ws_last_candle = connect(uri=f'{self.ws_server}/trade-api-market-data-connector/api/v1/market-data/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('LastCandles', self.ws_last_candle, self.on_candle), name='LastCandlesThread').start()  # Создаем и запускаем поток
        request = {'subscribeType': subscribe_type, 'dataType': 1, 'instruments': instruments, 'timeFrame': time_frame}  # Формируем запрос
        self.logger.debug(f'Подписка на последние свечи: {request}')
        self.ws_last_candle.send(dumps(request))  # Переводим JSON в строку, отправляем запрос

    def unsubscribe_last_candles(self):
        """Отмена подписки на последние свечи"""
        if self.ws_last_candle is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на последние свечи')
            self.ws_last_candle.close()  # то закрваем соединение
            self.ws_last_candle = None  # Сбрасываем подключение

    def get_candles_chart(self, class_code: str, ticker: str, start_date: datetime, end_date: datetime, time_frame: str):  # https://trade-api.bcs.ru/market-data/candles
        """Исторические свечи

        :param str class_code: Режим торгов
        :param str ticker: Тикер
        :param datetime start_date: Время начала периода
        :param datetime end_date: Время окончания периода
        :param str time_frame: Временной интервал (M1, M5, M15, M30, H1, H4, D, W, MN)
        """
        params = {'classCode': class_code, 'ticker': ticker, 'startDate': start_date.isoformat(), 'endDate': end_date.isoformat(), 'timeFrame': time_frame}
        return self._check_result(get(url=f'{self.http_server}/trade-api-market-data-connector/api/v1/candles-chart', params=params, headers=self._get_headers()))

    # Стакан https://trade-api.bcs.ru/market-data/order-book

    def subscribe_order_book(self, subscribe_type: int, instruments: list[dict[str, str]], depth: int = 20):
        """Подписка на стакан

        :param int subscribe_type: 0 - подписка, 1 - отмена подписки
        :param list[dict[str, str]] instruments: Список инструментов. instruments = [{'classCode': 'TQBR', 'ticker': 'SBER'}, {'classCode': 'SPBFUT', 'ticker': 'CNYRUBF'}]
        :param int depth: Глубина стакана 1-20
        """
        if self.ws_order_book is None:  # Если не подписаны
            self.ws_order_book = connect(uri=f'{self.ws_server}/trade-api-market-data-connector/api/v1/market-data/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('OrderBook', self.ws_order_book, self.on_order_book), name='OrderBookThread').start()  # Создаем и запускаем поток
        request = {'subscribeType': subscribe_type, 'dataType': 0, 'instruments': instruments, 'depth': depth}  # Формируем запрос
        self.logger.debug(f'Подписка на стакан: {request}')
        self.ws_order_book.send(dumps(request))  # Переводим JSON в строку, отправляем запрос

    def unsubscribe_order_book(self):
        """Отмена подписки на стаан"""
        if self.ws_order_book is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на стакан')
            self.ws_order_book.close()  # то закрваем соединение
            self.ws_order_book = None  # Сбрасываем подключение

    # Обезличенные сделки https://trade-api.bcs.ru/market-data/trades

    def subscribe_trades(self, subscribe_type: int, instruments: list[dict[str, str]]):
        """Подписка на обезличенные сделки

        :param int subscribe_type: 0 - подписка, 1 - отмена подписки
        :param list[dict[str, str]] instruments: Список инструментов. instruments = [{'classCode': 'TQBR', 'ticker': 'SBER'}, {'classCode': 'SPBFUT', 'ticker': 'CNYRUBF'}]
        """
        if self.ws_trades is None:  # Если не подписаны
            self.ws_trades = connect(uri=f'{self.ws_server}/trade-api-market-data-connector/api/v1/market-data/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Trades', self.ws_trades, self.on_trade), name='TradesThread').start()  # Создаем и запускаем поток
        request = {'subscribeType': subscribe_type, 'dataType': 2, 'instruments': instruments}  # Формируем запрос
        self.logger.debug(f'Подписка на обезличенные сделки: {request}')
        self.ws_trades.send(dumps(request))  # Переводим JSON в строку, отправляем запрос

    def unsubscribe_trades(self):
        """Отмена подписки на обезличенные сделки"""
        if self.ws_trades is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на обезличенные сделки')
            self.ws_trades.close()  # то закрваем соединение
            self.ws_trades = None  # Сбрасываем подключение

    # Справочник https://trade-api.bcs.ru/information

    def get_instrument_ticker(self, tickers: list[str]) -> list[dict]:  # https://trade-api.bcs.ru/information/instrument-by-ticker
        """Поиск инструмента по тикеру

        :param list[str] tickers: Список тикеров
        """
        params = {'tickers': tickers}
        return self._check_result(post(url=f'{self.http_server}/trade-api-information-service/api/v1/instruments/by-tickers', json=params, headers=self._get_headers()))

    def get_instrument_type(self, ticker_type: str, base_asset_ticker: str):  # https://trade-api.bcs.ru/information/instrument-by-type
        """Поиск инструмента по типу инструмента

        :param str ticker_type: Тип инструмента (CURRENCY, STOCK, FOREIGN_STOCK, BONDS, NOTES, DEPOSITARY_RECEIPTS, EURO_BONDS, MUTUAL_FUNDS, ETF, FUTURES, OPTIONS, GOODS, INDICES)
        :param str base_asset_ticker: Тикер базового актива. Обязательно, если ticker_type = OPTIONS
        """
        params = {'type': ticker_type, 'baseAssetTicker': base_asset_ticker}
        return self._check_result(get(url=f'{self.http_server}/trade-api-information-service/api/v1/instruments/by-type', params=params, headers=self._get_headers()))

    def get_daily_schedule(self, class_code: str, ticker: str):  # https://trade-api.bcs.ru/information/schedule
        """Расписание инструмента

        :param str class_code: Режим торгов
        :param str ticker: Тикер
        """
        params = {'classCode': class_code, 'ticker': ticker}
        return self._check_result(get(url=f'{self.http_server}/trade-api-information-service/api/v1/trading-schedule/daily-schedule', params=params, headers=self._get_headers()))

    def get_trading_status(self, class_code: str):  # https://trade-api.bcs.ru/information/trading-status
        """Торговый статус инструмента

        :param str class_code: Режим торгов
        """
        params = {'classCode': class_code}
        return self._check_result(get(url=f'{self.http_server}/trade-api-information-service/api/v1/trading-schedule/status', params=params, headers=self._get_headers()))

    # Маржинальные показатели https://trade-api.bcs.ru/marginal-indicators

    def subscribe_margins(self):  # https://trade-api.bcs.ru/marginal-indicators/marginal-indicators
        """Подписка на маржинальные показатели портфеля"""
        if self.ws_margins is None:  # Если не подписаны
            self.logger.debug('Подписка на маржинальные показатели портфеля')
            self.ws_margins = connect(uri=f'{self.ws_server}/trade-api-bff-marginal-indicators/api/v1/marginal-indicators/ws', additional_headers=self._get_headers())  # Подключаемся к сервису WebSockets
            Thread(target=self._subscribe_thread, args=('Margins', self.ws_margins, self.on_margin), name='MarginssThread').start()  # Создаем и запускаем поток

    def unsubscribe_margins(self):
        """Отмена подписки на маржинальные показатели портфеля"""
        if self.ws_margins is not None:  # Если подписаны
            self.logger.debug('Отмена подписки на маржинальные показатели портфеля')
            self.ws_margins.close()  # то закрваем соединение
            self.ws_margins = None  # Сбрасываем подключение

    def get_instruments_discounts(self):  # https://trade-api.bcs.ru/marginal-indicators/discounts
        """Получение дисконтов"""
        return self._check_result(get(url=f'{self.http_server}/trade-api-bff-marginal-indicators/api/v1/instruments-discounts', headers=self._get_headers()))

    # Запросы REST

    def _get_headers(self):
        """Получение хедеров для запросов"""
        return {'Authorization': f'Bearer {self.get_access_token()}'}

    def _check_result(self, response):
        """Анализ результата запроса

        :param Response response: Результат запроса
        :return: Справочник из JSON, текст, None в случае веб ошибки
        """
        if not response:  # Если ответ не пришел. Например, при таймауте
            self.logger.error('Ошибка запроса: Таймаут')  # Событие ошибки
            return None  # то возвращаем пустое значение
        content = response.content.decode('utf-8')  # Результат запроса
        if response.status_code != 200:  # Если статус ошибки
            self.logger.error(f'Ошибка запроса: {response.status_code} Запрос: {response.request.path_url} Ответ: {content}')  # Событие ошибки
            return None  # то возвращаем пустое значение
        self.logger.debug(f'Запрос : {response.request.path_url}')
        self.logger.debug(f'Ответ  : {content}')
        try:
            return loads(content)  # Декодируем JSON в справочник, возвращаем его. Ошибки также могут приходить в виде JSON
        except JSONDecodeError:  # Если произошла ошибка при декодировании JSON, например, при удалении заявок
            return content  # то возвращаем значение в виде текста

    # Подписки WebSocket

    def _subscribe_thread(self, name: str, ws: ClientConnection, event):
        """Поток подписки"""
        while True:  # Пока получаем данные
            try:
                response_json = ws.recv()  # Ожидаем ответ
                response = loads(response_json)  # Переводим JSON в словарь
                self.logger.debug(f'Данные подписки : {response}')
                event.trigger(response)  # Вызываем событие
            except ConnectionClosed:  # Событие закрытия соединения
                return  # Выходим, дальше не продолжаем
            except Exception as ex:  # При других типах ошибок
                self.logger.error(f'{name}: Ошибка получения подписки {ex}')
                return  # Выходим, дальше не продолжаем

    # Выход и закрытие

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Выход из класса, например, с with"""
        self.close_web_socket()  # Закрываем соединение с сервером WebSocket

    def __del__(self):
        self.close_web_socket()  # Закрываем соединение с сервером WebSocket

    def close_web_socket(self):
        """Закрытие соединения с сервером WebSocket"""
        self.unsubscribe_limits()  # Лимиты
        self.unsubscribe_portfolio()  # Портфель
        self.unsubscribe_executions()  # Исполненные заявки
        self.unsubscribe_transactions()  # Статус созданных заявок
        self.unsubscribe_quotes()  # Котировки
        self.unsubscribe_last_candles()  # Последние свечи
        self.unsubscribe_order_book()  # Стакан
        self.unsubscribe_trades()  # Обезличенные сделки
        self.unsubscribe_margins()  # Маржинальные показатели

    # Функции конвертации

    def dataname_to_class_code_ticker(self, dataname) -> tuple[str | None, str]:
        """Код режима торгов и тикер из названия тикера

        :param str dataname: Название тикера
        :return: Код режима торгов и тикер
        """
        symbol_parts = dataname.split('.')  # По разделителю пытаемся разбить тикер на части
        if len(symbol_parts) >= 2:  # Если тикер задан в формате <Код режима торгов>.<Код тикера>
            class_code = symbol_parts[0]  # Код режима торгов
            ticker = '.'.join(symbol_parts[1:])  # Код тикера
        else:  # Если тикер задан без кода режима торгов
            ticker = dataname  # Код тикера
            si = self.get_instrument_ticker([ticker])  # Информация о тикере
            class_code = None if len(si) == 0 else si[0]['boards'][0]['classCode']  # Код режима торгов
        return class_code, ticker

    @staticmethod
    def class_code_ticker_to_dataname(class_code, ticker) -> str:
        """Название тикера из кода режима торгов и тикера

        :param str class_code: Код режима торгов
        :param str ticker: Тикер
        :return: Название тикера
        """
        return f'{class_code}.{ticker}'

    def msk_to_utc_datetime(self, dt, tzinfo=False) -> datetime:
        """Перевод времени из московского в UTC

        :param datetime dt: Московское время
        :param bool tzinfo: Отображать временнУю зону
        :return: Время UTC
        :rtype: datetime
        """
        dt_msk = dt.replace(tzinfo=self.tz_msk)  # Заданное время ставим в зону МСК
        dt_utc = dt_msk.astimezone(timezone.utc)  # Переводим в зону UTC
        return dt_utc if tzinfo else dt_utc.replace(tzinfo=None)

    def utc_to_msk_datetime(self, dt, tzinfo=False) -> datetime:
        """Перевод времени из UTC в московское

        :param datetime dt: Время UTC
        :param bool tzinfo: Отображать временнУю зону
        :return: Московское время
        :rtype: datetime
        """
        dt_utc = dt.replace(tzinfo=timezone.utc)  # Заданное время ставим в зону UTC
        dt_msk = dt_utc.astimezone(self.tz_msk)  # Переводим в зону МСК
        return dt_msk if tzinfo else dt_msk.replace(tzinfo=None)

    def get_long_token_from_keyring(self, service: str, username: str) -> str | None:
        """Получение токена из системного хранилища keyring по частям"""
        try:
            index = 0  # Номер части токена
            token_parts = []  # Части токена
            while True:  # Пока есть части токена
                token_part = keyring.get_password(service, f'{username}{index}')  # Получаем часть токена
                if token_part is None:  # Если части токена нет
                    break  # то выходим, дальше не продолжаем
                token_parts.append(token_part)  # Добавляем часть токена
                index += 1  # Переходим к следующей части токена
            if not token_parts:  # Если токен не найден
                self.logger.error(f'Токен не найден в системном хранилище. Вызовите bp_provider = BCSPy("<Токен>")')
                return None
            token = ''.join(token_parts)  # Собираем токен из частей
            self.logger.debug('Токен успешно загружен из системного хранилища')
            return token
        except keyring.errors.KeyringError as e:
            self.logger.fatal(f'Ошибка доступа к системному хранилищу: {e}')
        except Exception as e:
            self.logger.fatal(f'Ошибка при загрузке токена: {e}')

    def set_long_token_to_keyring(self, service: str, username: str, token: str, password_split_size: int = 500) -> None:
        """Установка токена в системное хранилище keyring по частям"""
        try:
            self.clear_long_token_from_keyring(service, username)  # Очищаем предыдущие части токена
            token_parts = [token[i:i + password_split_size] for i in range(0, len(token), password_split_size)]  # Разбиваем токен на части заданного размера
            for index, token_part in enumerate(token_parts):  # Пробегаемся по частям токена
                keyring.set_password(service, f'{username}{index}', token_part)  # Сохраняем часть токена
            self.logger.debug(f'Частей сохраненного токена в хранилище: {len(token_parts)}')
        except keyring.errors.KeyringError as e:
            self.logger.fatal(f'Ошибка сохранения в системное хранилище: {e}')
        except Exception as e:
            self.logger.fatal(f'Ошибка при сохранении токена: {e}')

    def clear_long_token_from_keyring(self, service: str, username: str) -> None:
        """Удаление всех частей токена из системного хранилища keyring"""
        try:
            index = 0  # Номер части токена
            while True:  # Пока есть части токена
                if keyring.get_password(service, f'{username}{index}') is None:  # Если части токена нет
                    break  # то выходим, дальше не продолжаем
                keyring.delete_password(service, f'{username}{index}')  # Удаляем часть токена
                index += 1  # Переходим к следующей части токена
        except keyring.errors.KeyringError as e:
            self.logger.fatal(f'Ошибка доступа к системному хранилищу: {e}')


class Event:
    """Событие с подпиской / отменой подписки"""
    def __init__(self):
        self._callbacks: set[Any] = set()  # Избегаем дубликатов функций при помощи set

    def subscribe(self, callback) -> None:
        """Подписаться на событие"""
        self._callbacks.add(callback)  # Добавляем функцию в список

    def unsubscribe(self, callback) -> None:
        """Отписаться от события"""
        self._callbacks.discard(callback)  # Удаляем функцию из списка. Если функции нет в списке, то не будет ошибки

    def trigger(self, *args, **kwargs) -> None:
        """Вызвать событие"""
        for callback in list(self._callbacks):  # Пробегаемся по копии списка, чтобы избежать исключения при удалении
            callback(*args, **kwargs)  # Вызываем функцию
