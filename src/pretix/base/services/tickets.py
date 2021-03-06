import logging
import os

from django.core.files.base import ContentFile
from django.utils.timezone import now
from django.utils.translation import ugettext as _

from pretix.base.i18n import language
from pretix.base.models import (
    CachedCombinedTicket, CachedTicket, Event, InvoiceAddress, Order,
    OrderPosition,
)
from pretix.base.services.tasks import ProfiledTask
from pretix.base.settings import PERSON_NAME_SCHEMES
from pretix.base.signals import allow_ticket_download, register_ticket_outputs
from pretix.celery_app import app
from pretix.helpers.database import rolledback_transaction

logger = logging.getLogger(__name__)


def generate_orderposition(order_position: int, provider: str):
    order_position = OrderPosition.objects.select_related('order', 'order__event').get(id=order_position)

    with language(order_position.order.locale):
        responses = register_ticket_outputs.send(order_position.order.event)
        for receiver, response in responses:
            prov = response(order_position.order.event)
            if prov.identifier == provider:
                filename, ttype, data = prov.generate(order_position)
                path, ext = os.path.splitext(filename)
                for ct in CachedTicket.objects.filter(order_position=order_position, provider=provider):
                    ct.delete()
                ct = CachedTicket.objects.create(order_position=order_position, provider=provider,
                                                 extension=ext, type=ttype, file=None)
                ct.file.save(filename, ContentFile(data))
                return ct.pk


def generate_order(order: int, provider: str):
    order = Order.objects.select_related('event').get(id=order)

    with language(order.locale):
        responses = register_ticket_outputs.send(order.event)
        for receiver, response in responses:
            prov = response(order.event)
            if prov.identifier == provider:
                filename, ttype, data = prov.generate_order(order)
                path, ext = os.path.splitext(filename)
                for ct in CachedCombinedTicket.objects.filter(order=order, provider=provider):
                    ct.delete()
                ct = CachedCombinedTicket.objects.create(order=order, provider=provider, extension=ext,
                                                         type=ttype, file=None)
                ct.file.save(filename, ContentFile(data))
                return ct.pk


@app.task(base=ProfiledTask)
def generate(model: str, pk: int, provider: str):
    if model == 'order':
        return generate_order(pk, provider)
    elif model == 'orderposition':
        return generate_orderposition(pk, provider)


class DummyRollbackException(Exception):
    pass


def preview(event: int, provider: str):
    event = Event.objects.get(id=event)

    with rolledback_transaction(), language(event.settings.locale):
        item = event.items.create(name=_("Sample product"), default_price=42.23,
                                  description=_("Sample product description"))
        item2 = event.items.create(name=_("Sample workshop"), default_price=23.40)

        from pretix.base.models import Order
        order = event.orders.create(status=Order.STATUS_PENDING, datetime=now(),
                                    email='sample@pretix.eu',
                                    locale=event.settings.locale,
                                    expires=now(), code="PREVIEW1234", total=119)

        scheme = PERSON_NAME_SCHEMES[event.settings.name_scheme]
        sample = {k: str(v) for k, v in scheme['sample'].items()}
        p = order.positions.create(item=item, attendee_name_parts=sample, price=item.default_price)
        order.positions.create(item=item2, attendee_name_parts=sample, price=item.default_price, addon_to=p)
        order.positions.create(item=item2, attendee_name_parts=sample, price=item.default_price, addon_to=p)

        InvoiceAddress.objects.create(order=order, name_parts=sample, company=_("Sample company"))

        responses = register_ticket_outputs.send(event)
        for receiver, response in responses:
            prov = response(event)
            if prov.identifier == provider:
                return prov.generate(p)


def get_tickets_for_order(order):
    can_download = all([r for rr, r in allow_ticket_download.send(order.event, order=order)])
    if not can_download:
        return []
    if not order.ticket_download_available:
        return []

    providers = [
        response(order.event)
        for receiver, response
        in register_ticket_outputs.send(order.event)
    ]

    tickets = []

    for p in providers:
        if not p.is_enabled:
            continue

        if p.multi_download_enabled:
            try:
                ct = CachedCombinedTicket.objects.filter(
                    order=order, provider=p.identifier, file__isnull=False
                ).last()
                if not ct or not ct.file:
                    retval = generate.apply(args=('order', order.pk, p.identifier))
                    ct = CachedCombinedTicket.objects.get(pk=retval.get())
                tickets.append((
                    "{}-{}-{}{}".format(
                        order.event.slug.upper(), order.code, ct.provider, ct.extension,
                    ),
                    ct
                ))
            except:
                logger.exception('Failed to generate ticket.')
        else:
            for pos in order.positions.all():
                if pos.addon_to and not order.event.settings.ticket_download_addons:
                    continue
                if not pos.item.admission and not order.event.settings.ticket_download_nonadm:
                    continue
                try:
                    ct = CachedTicket.objects.filter(
                        order_position=pos, provider=p.identifier, file__isnull=False
                    ).last()
                    if not ct or not ct.file:
                        retval = generate.apply(args=('orderposition', pos.pk, p.identifier))
                        ct = CachedTicket.objects.get(pk=retval.get())
                    tickets.append((
                        "{}-{}-{}-{}{}".format(
                            order.event.slug.upper(), order.code, pos.positionid, ct.provider, ct.extension,
                        ),
                        ct
                    ))
                except:
                    logger.exception('Failed to generate ticket.')

    return tickets
