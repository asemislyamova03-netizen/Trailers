import requests


class KaspiClientError(Exception):
    pass


class KaspiShopClient:
    def __init__(self, token: str | None, base_url: str = 'https://kaspi.kz/shop/api/v2', timeout: int = 60):
        self.token = (token or '').strip()
        self.base_url = (base_url or '').rstrip('/')
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict:
        if not self.token:
            raise KaspiClientError('Не задан KASPI_SHOP_TOKEN на сервере.')
        return {
            'Content-Type': 'application/vnd.api+json',
            'X-Auth-Token': self.token,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f'{self.base_url}/{path.lstrip("/")}'
        try:
            response = requests.get(url, headers=self._headers(), params=params or {}, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise KaspiClientError(f'Ошибка запроса Kaspi: {exc}') from exc
        try:
            return response.json()
        except ValueError as exc:
            raise KaspiClientError('Kaspi вернул не JSON-ответ.') from exc

    def get_order_by_code(self, order_code: str) -> dict | None:
        code = (order_code or '').strip()
        if not code:
            raise KaspiClientError('Не указан номер заказа Kaspi.')
        payload = self._get('orders', {
            'page[number]': 0,
            'page[size]': 20,
            'filter[orders][code]': code,
            'include[orders]': 'user',
        })
        data = payload.get('data') or []
        return data[0] if data else None

    def list_orders(
        self,
        state: str = 'NEW',
        creation_from_ms: int | None = None,
        creation_to_ms: int | None = None,
        status: str | None = None,
        page_number: int = 0,
        page_size: int = 20,
    ) -> dict:
        params = {
            'page[number]': max(int(page_number or 0), 0),
            'page[size]': min(max(int(page_size or 20), 1), 100),
            'filter[orders][state]': state or 'NEW',
            'include[orders]': 'user',
        }
        if creation_from_ms:
            params['filter[orders][creationDate][$ge]'] = creation_from_ms
        if creation_to_ms:
            params['filter[orders][creationDate][$le]'] = creation_to_ms
        if status:
            params['filter[orders][status]'] = status
        return self._get('orders', params)

    def get_order_entries(self, order_id: str) -> list[dict]:
        payload = self._get(f'orders/{order_id}/entries')
        return payload.get('data') or []
