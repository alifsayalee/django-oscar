from decimal import Decimal as D

from oscar.apps.partner import prices, strategy

from .gateway import configured_currency


class ConfiguredCurrencyStrategy(strategy.Default):
    """
    Oscar's default strategy, but priced in the configured PayPal currency.

    Amounts come from the catalogue (the stock record price); the currency the
    order is recorded and charged in comes from ``PAYPAL_CURRENCY``.
    """

    def pricing_policy(self, product, stockrecord):
        if not stockrecord or stockrecord.price is None:
            return prices.Unavailable()
        return prices.FixedPrice(
            currency=configured_currency(), excl_tax=stockrecord.price, tax=D("0.00")
        )

    def parent_pricing_policy(self, product, children_stock):
        stockrecords = [x[1] for x in children_stock if x[1] is not None]
        if not stockrecords:
            return prices.Unavailable()
        return prices.FixedPrice(
            currency=configured_currency(),
            excl_tax=stockrecords[0].price,
            tax=D("0.00"),
        )
