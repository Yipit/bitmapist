# -*- coding: utf-8 -*-
"""
bitmapist
~~~~~~~~~
Implements a powerful analytics library on top of Redis's support for bitmaps and bitmap operations.

This library makes it possible to implement real-time, highly scalable analytics that can answer following questions:

* Has user 123 been online today? This week? This month?
* Has user 123 performed action "X"?
* How many users have been active have this month? This hour?
* How many unique users have performed action "X" this week?
* How many % of users that were active last week are still active?
* How many % of users that were active last month are still active this month?

This library is very easy to use and enables you to create your own reports easily.

Using Redis bitmaps you can store events for millions of users in a very little amount of memory (megabytes).
You should be careful about using huge ids (e.g. 2^32 or bigger) as this could require larger amounts of memory.

If you want to read more about bitmaps please read following:
* http://blog.getspool.com/2011/11/29/fast-easy-realtime-metrics-using-redis-bitmaps/
* http://redis.io/commands/setbit
* http://en.wikipedia.org/wiki/Bit_array
* http://www.slideshare.net/crashlytics/crashlytics-on-redis-analytics

Requires Redis 2.6+ and newest version of redis-py.

Examples
========

Setting things up::

    from datetime import datetime, timedelta
    from bitmapist import Bitmapist

    bm = Bitmapist(
        redis_client=redis.Redis('localhost', 6379),
        prefix='trackist',
        divider=':')

    now = datetime.utcnow()
    last_month = datetime.utcnow() - timedelta(days=30)

Mark user 123 as active::

    bm.mark_event('active', 123)

Mark user 123 as a paid_user:

    bm.mark_attribute('paid_user', 123)

Answer if user 123 has been active this month::

    assert 123 in bm.get_month_event('active', now)

How many users have been active this week?::

    print len(bm.get_week_event('active', now))

Perform bit operations. Which users that have been active last month are still active this month?::

    active_2_months = bm.bit_op_and(
        bm.get_month_event('active', last_month),
        bm.get_month_event('active', now),
    )

Nest bit operations!::

    active_2_months = bm.bit_op_and(
        bm.bit_op_and(
            bm.get_month_event('active', last_month),
            bm.get_month_event('active', now),
        ),
        bm.get_attribute('paid_user')
    )

:copyright: 2012 by Doist Ltd.
:developer: Amir Salihefendic ( http://amix.dk )
:license: BSD
"""

import re

from datetime import datetime


class Bitmapist(object):

    def __init__(self, redis_client, prefix='trackist', divider=':', temp_ttl=None):
        """
        :param :temp_ttl Time to live for temporary bit op keys. Defaults to 60 seconds

        """
        self.redis_client = redis_client
        self.prefix = prefix
        self.divider = divider
        self.temp_ttl = temp_ttl or 60

    def get_month_event(self, event_name, now):
        return MonthEvents(event_name, now.year, now.month, self.prefix, self.divider, self.redis_client)

    def get_week_event(self, event_name, now):
        return WeekEvents(event_name, now.isocalendar()[0], now.isocalendar()[1], self.prefix, self.divider, self.redis_client)

    def get_day_event(self, event_name, now):
        return DayEvents(event_name, now.year, now.month, now.day, self.prefix, self.divider, self.redis_client)

    def get_hour_event(self, event_name, now):
        return HourEvents(event_name, now.year, now.month, now.day, now.hour, self.prefix, self.divider, self.redis_client)

    def get_attribute(self, attribute_name):
        return Attributes(attribute_name, self.prefix, self.divider, self.redis_client)

    def bit_op_and(self, *bitmaps):
        return BitOpAnd(self.prefix, self.divider, self.redis_client, self.temp_ttl, *bitmaps)

    def bit_op_or(self, *bitmaps):
        return BitOpOr(self.prefix, self.divider, self.redis_client, self.temp_ttl, *bitmaps)

    def bit_op_xor(self, *bitmaps):
        return BitOpXor(self.prefix, self.divider, self.redis_client, self.temp_ttl, *bitmaps)

    def bit_op_not(self, bitmap):
        return BitOpNot(self.prefix, self.divider, self.redis_client, self.temp_ttl, bitmap)

    #--- Events marking and deleting ----------------------------------------------
    def mark_event(self, event_name, uuid, now=None, month=True, week=True, day=True, hour=True,
            month_ttl=None, week_ttl=None, day_ttl=None, hour_ttl=None):
        """
        Marks an event for hours, days, weeks and months.

        :param :event_name The name of the event, could be "active" or "new_signups"
        :param :uuid An unique id, typically user id. The id should not be huge, read Redis documentation why (bitmaps)
        :param :now Which date should be used as a reference point, default is `datetime.utcnow`
        :param :month Whether the month granularity for the event should be saved
        :param :week Whether the week granularity for the event should be saved
        :param :day Whether the day granularity for the event should be saved
        :param :hour Whether the hour granularity for the event should be saved
        :param :month_ttl Time to live for the month key, in seconds or timedelta
        :param :week_ttl Time to live for the week key, in seconds or timedelta
        :param :day_ttl Time to live for the day key, in seconds or timedelta
        :param :hour_ttl Time to live for the hour key, in seconds or timedelta

        Examples::

            # Mark id 1 as active
            bm.mark_event('active', 1)

            # Mark task completed for id 252
            bm.mark_event('tasks_completed', 252)

            # Mark month and week key only for active user
            bm.mark_event('active', 1, day=False, hour=False)
        """
        if not now:
            now = datetime.utcnow()

        stat_objs = []
        if month:
            stat_objs.append((self.get_month_event(event_name, now), month_ttl))
        if week:
            stat_objs.append((self.get_week_event(event_name, now), week_ttl))
        if day:
            stat_objs.append((self.get_day_event(event_name, now), day_ttl))
        if hour:
            stat_objs.append((self.get_hour_event(event_name, now), hour_ttl))

        with self.redis_client.pipeline() as p:
            p.multi()
            for obj, ttl in stat_objs:
                p.setbit(obj.redis_key, uuid, 1)
                if ttl is not None:
                    p.expire(obj.redis_key, ttl)
            p.execute()

    def mark_attribute_multi(self, attribute_name, uuids, mark_as=1):
        if mark_as not in (0, 1):
            raise ValueError('Can only mark bitmaps with 0 or 1')

        obj = self.get_attribute(attribute_name)
        with self.redis_client.pipeline(transaction=False) as p:
            p.multi()
            for _id in uuids:
                p.setbit(obj.redis_key, _id, mark_as)
            p.execute()

    def mark_attribute(self, attribute_name, uuid, mark_as=1):
        """
        Marks an attribute that is not time specific.

        :param :attribute_name The name of the attribute, e.g. "paid_user" or "new_email_split_test_group"
        :param :uuid An unique id, typically user id. The id should not be huge, read Redis documentation why (bitmaps)
        :param :mark_as An int of 1 or 0 to set uuid to

        Examples::

            # Mark id 1 as a paid user
            bm.mark_attribute('paid_user', 1)
        """

        if mark_as not in (0, 1):
            raise ValueError('Can only mark bitmaps with 0 or 1')

        if type(uuid) is list:
            return self.mark_attribute_multi(attribute_name, uuid, mark_as)

        obj = self.get_attribute(attribute_name)
        self.redis_client.setbit(obj.redis_key, uuid, mark_as)

    def get_all_event_names(self):
        """
        Returns all event names based on keys in the system,
        assuming they were generated by this bitmapist configuration
        """
        client = self.redis_client
        keys = client.keys('{0}{1}ev{1}*'.format(self.prefix, self.divider))
        event_names = set([])
        # Assumes all events create a WeekEvent
        event_re = re.compile(
            r'{0}ev{0}(.*){0}W\d+-\d+'.format(self.divider))
        for key in keys:
            match = event_re.search(key)
            if match:
                event_names.add(match.groups()[0])
        return event_names

    def get_all_attribute_names(self):
        """
        Returns all attribute names assuming based on keys in the system,
        assuming they were generated by bitmapist
        """
        client = self.redis_client
        keys = client.keys('{0}{1}at{1}*'.format(self.prefix, self.divider))
        attr_names = set([])
        thing = re.compile(r'{0}at{0}(.*)'.format(self.divider))
        for key in keys:
            match = thing.search(key)
            if match:
                attr_names.add(match.groups()[0])
        return attr_names

    def delete_all(self):
        """
        Delete all bitmap keys from the database.
        """
        cli = self.redis_client
        keys = cli.keys('%s%s*' % (self.prefix, self.divider))
        if len(keys) > 0:
            cli.delete(*keys)

    def delete_all_events(self):
        """
        Delete all events from the database.
        """
        cli = self.redis_client
        keys = cli.keys('%s%sev%s*' % (self.prefix, self.divider, self.divider))
        if len(keys) > 0:
            cli.delete(*keys)

    def delete_all_attributes(self):
        """
        Delete all attributes from the database.
        """
        cli = self.redis_client
        keys = cli.keys('%s%sat%s*' % (self.prefix, self.divider, self.divider))
        if len(keys) > 0:
            cli.delete(*keys)

    def delete_temporary_bitop_keys(self):
        """
        Delete all temporary keys that are used when using bit operations.
        """
        cli = self.redis_client
        keys = cli.keys('%s%sbitop%s*' % (self.prefix, self.divider, self.divider))
        if len(keys) > 0:
            cli.delete(*keys)


#--- Events ----------------------------------------------
class MixinMarked:
    """
    Extends with an obj.has_events_marked()
    that returns `True` if there are any events marked,
    otherwise `False` is returned.
    """
    def has_events_marked(self):
        cli = self.redis_client
        return cli.get(self.redis_key) != None


class MixinCounts:
    """
    Extends with an obj.get_count() that uses BITCOUNT to
    count all the events. Supports also __len__
    """

    ERROR = 'error'

    def get_count(self, start_bit=None, end_bit=None):
        """
        Redis bitcount command start/end paramaters use Byte units
        whereas setbit operations are on a bit basis
        We can either approximate or use this hack to determine exact count

        :param :start_bit Starting bit, inclusive
        :param :end_bit Ending bit, inclusive
        """
        # TODO: deal with errors in what is passed in

        cli = self.redis_client
        if not start_bit and not end_bit:
            return cli.bitcount(self.redis_key)

        start_byte = self._convert_to_start_byte(start_bit)   # First byte that is entirely
                                                              # in the range of bits specified
        end_byte = self._convert_to_end_byte(end_bit)         # Last byte that is entirely
                                                              # in the range of bits specified
        total_bits = 0

        # TODO: pipeline this
        if start_byte != self.ERROR and end_byte != self.ERROR:

            # Bits before the start_byte
            bit_floor = start_byte * 8
            for offset in xrange(start_bit, bit_floor, (start_bit >= 0 and 1 or -1)):
                total_bits += cli.getbit(self.redis_key, offset)

            # Bits that fall inside the bytes
            total_bits += cli.bitcount(self.redis_key, start=start_byte, end=end_byte)

            # Bits after end_byte
            bit_ceiling = (end_byte + 1) * 8
            for offset in xrange(bit_ceiling, end_bit + 1, (bit_ceiling >= 0 and 1 or -1)):
                total_bits += cli.getbit(self.redis_key, offset)
        else:
            for offset in xrange(start_bit, end_bit + 1):
                total_bits += cli.getbit(self.redis_key, offset)

        return total_bits

    def _convert_to_start_byte(self, bit):
        if -7 <= bit < 0:
            return self.ERROR
        else:
            return (bit + 7) / 8

    def _convert_to_end_byte(self, bit):
        if bit < 0:
            return (bit + 1) / 8 - 1
        elif bit < 7:
            return self.ERROR
        else:
            return (bit - 7) / 8

    def __len__(self):
        return self.get_count()


class MixinContains:
    """
    Makes it possible to see if an uuid has been marked.

    Example::

       user_active_today = 123 in DayEvents('active', 2012, 10, 23)
    """
    def __contains__(self, uuid):
        cli = self.redis_client
        if cli.getbit(self.redis_key, uuid):
            return True
        else:
            return False


class Bitmap(object, MixinCounts, MixinContains, MixinMarked):
    def __init__(self, redis_key, redis_client):
        self.redis_client = redis_client
        self.redis_key = redis_key


class MonthEvents(Bitmap):
    """
    Events for a month.

    Example::

        MonthEvents('active', 2012, 10)
    """
    def __init__(self, event_name, year, month, prefix, divider, redis_client):
        super(MonthEvents, self).__init__(
            _prefix_key(event_name, prefix, divider, '%s-%s' % (year, month)),
            redis_client)


class WeekEvents(Bitmap):
    """
    Events for a week.

    Example::

        WeekEvents('active', 2012, 48)
    """
    def __init__(self, event_name, year, week, prefix, divider, redis_client):
        super(WeekEvents, self).__init__(
            _prefix_key(event_name, prefix, divider, 'W%s-%s' % (year, week)),
            redis_client)


class DayEvents(Bitmap):
    """
    Events for a day.

    Example::

        DayEvents('active', 2012, 10, 23)
    """
    def __init__(self, event_name, year, month, day, prefix, divider, redis_client):
        super(DayEvents, self).__init__(
            _prefix_key(event_name, prefix, divider, '%s-%s-%s' % (year, month, day)),
            redis_client)


class HourEvents(Bitmap):
    """
    Events for a hour.

    Example::

        HourEvents('active', 2012, 10, 23, 13)
    """
    def __init__(self, event_name, year, month, day, hour, prefix, divider, redis_client):
        super(HourEvents, self).__init__(
            _prefix_key(event_name, prefix, divider, '%s-%s-%s-%s' % (year, month, day, hour)),
            redis_client)


class Attributes(Bitmap):
    """
    Attributes that are not time specific.

    Example::

        Attributes('paid_user')
    """
    def __init__(self, attribute_name, prefix, divider, redis_client):
        super(Attributes, self).__init__(
            _prefix_key(attribute_name, prefix, divider),
            redis_client)


#--- Bit operations ----------------------------------------------
class BitOperation(Bitmap):
    """
    Base class for bit operations (AND, OR, XOR).

    Please note that each bit operation creates a new key prefixed with `{KEY_PREFIX}{DIVIDER}bitop{DIVIDER}`.
    These temporary keys can be deleted with `delete_temporary_bitop_keys`.

    You can even nest bit operations.

    Example::

        active_2_months = BitOpAnd(
            MonthEvents('active', last_month.year, last_month.month),
            MonthEvents('active', now.year, now.month)
        )

    Example 2. Paid users that are active::

        active_2_months = BitOpAnd(
            MonthEvents('active', now.year, now.month),
            Attributes('paid_user')
        )

    Nested operations:

        active_2_months = BitOpAnd(
            BitOpAnd(
                MonthEvents('active', last_month.year, last_month.month),
                MonthEvents('active', now.year, now.month)
            ),
            MonthEvents('active', now.year, now.month)
        )

    """

    def __init__(self, op_name, prefix, divider, redis_client, ttl, *events):
        event_redis_keys = [ev.redis_key for ev in events]

        self.redis_key = divider.join([
            prefix,
            'bitop',
            op_name,
            '-'.join(event_redis_keys),
            ])

        self.redis_client = redis_client
        self.redis_client.bitop(op_name, self.redis_key, *event_redis_keys)
        self.redis_client.expire(self.redis_key, ttl)


class BitOpAnd(BitOperation):

    def __init__(self, prefix, divider, redis_client, ttl, *events):
        BitOperation.__init__(self, 'AND', prefix, divider, redis_client, ttl, *events)


class BitOpNot(BitOperation):

    def __init__(self, prefix, divider, redis_client, ttl, event):
        BitOperation.__init__(self, 'Not', prefix, divider, redis_client, ttl, event)


class BitOpOr(BitOperation):

    def __init__(self, prefix, divider, redis_client, ttl, *events):
        BitOperation.__init__(self, 'OR', prefix, divider, redis_client, ttl, *events)


class BitOpXor(BitOperation):

    def __init__(self, prefix, divider, redis_client, ttl, *events):
        BitOperation.__init__(self, 'XOR', prefix, divider, redis_client, ttl, *events)


#--- Private ----------------------------------------------
def _prefix_key(event_name, prefix, divider, date=None):
    if date:
        return divider.join([prefix, 'ev', event_name, date])
    else:
        return divider.join([prefix, 'at', event_name])
