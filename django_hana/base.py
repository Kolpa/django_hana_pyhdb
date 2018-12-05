"""
SAP HANA database backend for Django.
"""
import logging
import sys

from django.db import utils
from django.db.backends import *
from django.db.backends.signals import connection_created
from django_hana.operations import DatabaseOperations
from django_hana.client import DatabaseClient
from django_hana.creation import DatabaseCreation
from django_hana.introspection import DatabaseIntrospection
from django.utils.timezone import utc
from time import time

import pyhdb

logger = logging.getLogger('django.db.backends')


class DatabaseFeatures(BaseDatabaseFeatures):
    needs_datetime_string_cast = True
    can_return_id_from_insert = False
    requires_rollback_on_dirty_transaction = True
    has_real_datatype = True
    can_defer_constraint_checks = True
    has_select_for_update = True
    has_select_for_update_nowait = True
    has_bulk_insert = False
    supports_tablespaces = False
    supports_transactions = True
    can_distinct_on_fields = False
    uses_autocommit = True
    uses_savepoints = False
    can_introspect_foreign_keys = False
    supports_timezones = False


class CursorWrapper(object):
    """
        Hana doesn't support %s placeholders
        Wrapper to convert all %s placeholders to qmark(?) placeholders
    """
    codes_for_integrityerror = (301,)

    def __init__(self, cursor, db):
        self.cursor = cursor
        self.db = db
        self.is_hana = True

    def set_dirty(self):
        if not self.db.get_autocommit():
            self.db.set_dirty()

    def __getattr__(self, attr):
        self.set_dirty()
        if attr in self.__dict__:
            return self.__dict__[attr]
        else:
            return getattr(self.cursor, attr)

    def __iter__(self):
        return iter(self.cursor)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        # self.cursor.close()
        pass

    def execute(self, sql, params=()):
        print(sql)
        self.cursor.execute(sql, params)

    def executemany(self, sql, param_list):
        print(sql)
        self.cursor.executemany(sql, param_list)


class CursorDebugWrapper(CursorWrapper):

    def execute(self, sql, params=()):
        self.set_dirty()
        start = time()
        try:
            return CursorWrapper.execute(self, sql, params)
        finally:
            stop = time()
            duration = stop - start
            sql = self.db.ops.last_executed_query(self.cursor, sql, params)
            self.db.queries.append({
                'sql': sql,
                'time': "%.3f" % duration,
            })
            logger.debug('(%.3f) %s; args=%s' % (duration, sql, params),
                         extra={'duration': duration,
                                'sql': sql, 'params': params}
                         )

    def executemany(self, sql, param_list):
        self.set_dirty()
        start = time()
        try:
            return CursorWrapper.executemany(self, sql, param_list)
        finally:
            stop = time()
            duration = stop - start
            try:
                times = len(param_list)
            except TypeError:           # param_list could be an iterator
                times = '?'
            self.db.queries.append({
                'sql': '%s times: %s' % (times, sql),
                'time': "%.3f" % duration,
            })
            logger.debug('(%.3f) %s; args=%s' % (duration, sql, param_list),
                         extra={'duration': duration,
                                'sql': sql, 'params': param_list}
                         )


class DatabaseWrapper(BaseDatabaseWrapper):
    vendor = 'HANA'
    operators = {
        'exact': '= %s',
        'iexact': '= UPPER(%s)',
        'contains': 'LIKE %s',
        'icontains': 'LIKE UPPER(%s)',
        'regex': '~ %s',
        'iregex': '~* %s',
        'gt': '> %s',
        'gte': '>= %s',
        'lt': '< %s',
        'lte': '<= %s',
        'startswith': 'LIKE %s',
        'endswith': 'LIKE %s',
        'istartswith': 'LIKE UPPER(%s)',
        'iendswith': 'LIKE UPPER(%s)',
    }

    def __init__(self, *args, **kwargs):
        super(DatabaseWrapper, self).__init__(*args, **kwargs)

        self.settings_dict['ENGINE'] = 'mysql'

        self.features = DatabaseFeatures(self)

        self.ops = DatabaseOperations(self)
        self.client = DatabaseClient(self)
        self.creation = DatabaseCreation(self)
        self.introspection = DatabaseIntrospection(self)
        self.validation = BaseDatabaseValidation(self)

    def close(self):
        self.validate_thread_sharing()
        if self.connection is None:
            return
        self.connection.close()
        self.connection = None

    def connect(self):
        if not self.settings_dict['NAME']:
            from django.core.exceptions import ImproperlyConfigured
            raise ImproperlyConfigured(
                "settings.DATABASES is improperly configured. "
                "Please supply the NAME value.")
        conn_params = {}
        if self.settings_dict['USER']:
            conn_params['user'] = self.settings_dict['USER']
        if self.settings_dict['PASSWORD']:
            conn_params['password'] = self.settings_dict['PASSWORD']
        if self.settings_dict['HOST']:
            conn_params['host'] = self.settings_dict['HOST']
        if self.settings_dict['PORT']:
            conn_params['port'] = self.settings_dict['PORT']
        self.connection = pyhdb.connect(
            host=conn_params['host'],
            port=int(conn_params['port']),
            user=conn_params['user'],
            password=conn_params['password']
        )
        # set autocommit on by default
        self.connection.setautocommit(auto=True)
        self.default_schema = self.settings_dict['NAME']
        # make it upper case
        self.default_schema = self.default_schema.upper()
        self.create_or_set_default_schema()

    def _cursor(self):
        self.ensure_connection()
        return self.connection.cursor()

    def ensure_connection(self):
        if self.connection is None:
            self.connect()

    def cursor(self):
        # Call parent, in order to support cursor overriding from apps like Django Debug Toolbar
        # self.BaseDatabaseWrapper API is very asymetrical here - uses make_debug_cursor() for the
        # debug cursor, but directly instantiates urils.CursorWrapper for the regular one
        result = super(DatabaseWrapper, self).cursor()
        if getattr(result, 'is_hana', False):
            cursor = result
        else:
            cursor = CursorWrapper(self._cursor(), self)
        return cursor

    def make_debug_cursor(self, cursor):
        return CursorDebugWrapper(cursor, self)

    def create_or_set_default_schema(self):
        """
            create if doesn't exist and then make it default
        """
        cursor = self.cursor()
        cursor.execute(
            "select (1) as a from schemas where schema_name='%s'" % self.default_schema)
        res = cursor.fetchone()
        if not res:
            cursor.execute("create schema %s" % self.default_schema)
        cursor.execute("set schema "+self.default_schema)

    def _enter_transaction_management(self, managed):
        """
            Disables autocommit on entering a transaction
        """
        self.ensure_connection()
        if self.features.uses_autocommit and managed:
            self.connection.setautocommit(auto=False)

    def leave_transaction_management(self):
        """
            on leaving a transaction restore autocommit behavior
        """
        try:
            if self.transaction_state:
                del self.transaction_state[-1]
            else:
                raise TransactionManagementError("This code isn't under transaction "
                                                 "management")
            if self._dirty:
                self.rollback()
                raise TransactionManagementError("Transaction managed block ended with "
                                                 "pending COMMIT/ROLLBACK")
        except:
            raise
        finally:
            # restore autocommit behavior
            self.connection.setautocommit(auto=True)
        self._dirty = False

    def _commit(self):
        if self.connection is not None:
            return self.connection.commit()
