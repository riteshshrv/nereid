# -*- coding: utf-8 -*-
'''

    Add Translation support for Nereid

    :copyright: (c) 2013-14 by Openlabs Technologies & Consulting (P) Ltd.
    :copyright: (c) 2011-2012 NaN Projectes de Programari Lliure, S.L.
    :copyright: (c) 2014 MBSolutions
    :license: BSD, see LICENSE for more details

    This work is mostly inspired by the jasper_reports module of the tryton
    spain community.

'''
import os
import polib
import logging

import wtforms
from jinja2 import FileSystemLoader, Environment
from jinja2.ext import babel_extract, GETTEXT_FUNCTIONS
from babel.messages.extract import extract_from_dir
from babel.messages.extract import extract_from_file
from trytond.model import fields
from trytond.wizard import Wizard
from trytond.transaction import Transaction
from trytond.pool import Pool, PoolMeta
from trytond.cache import Cache
from trytond.tools import file_open, cursor_dict
from trytond.ir.translation import TrytonPOFile

__all__ = [
    'Translation',
    'TranslationSet',
    'TranslationUpdate',
    'TranslationClean',
]

NEREID_TRANSLATION_TYPES = [
    ('nereid_template', 'Nereid Template'),
    ('wtforms', 'WTforms built-in Messages'),
    ('nereid', 'Nereid Code'),
]

_nereid_types = [type[0] for type in NEREID_TRANSLATION_TYPES]

__metaclass__ = PoolMeta


class Translation:
    __name__ = 'ir.translation'

    comments = fields.Text(
        'Comments', readonly=True,
        help='Comments/Hints for translator'
    )

    @classmethod
    def __setup__(cls):
        super(Translation, cls).__setup__()
        for nereid_type in NEREID_TRANSLATION_TYPES:
            if nereid_type not in cls.type.selection:
                cls.type.selection.append(nereid_type)

    @property
    def unique_key(self):
        if self.type in _nereid_types:
            return (self.name, self.res_id, self.type, self.src)
        return super(Translation, self).unique_key

    @classmethod
    def translation_import(cls, lang, module, po_path):
        """
        Override the entire method: upstream code needs refactoring to allow
        for customization.
        Based on trytond version: 3.2.4
        """
        pool = Pool()
        ModelData = pool.get('ir.model.data')
        models_data = ModelData.search([
                ('module', '=', module),
                ])
        fs_id2prop = {}
        for model_data in models_data:
            fs_id2prop.setdefault(model_data.model, {})
            fs_id2prop[model_data.model][model_data.fs_id] = \
                (model_data.db_id, model_data.noupdate)
            for extra_model in cls.extra_model_data(model_data):
                fs_id2prop.setdefault(extra_model, {})
                fs_id2prop[extra_model][model_data.fs_id] = \
                    (model_data.db_id, model_data.noupdate)

        translations = set()
        to_create = []
        pofile = polib.pofile(po_path)

        id2translation = {}
        key2ids = {}
        module_translations = cls.search([
                ('lang', '=', lang),
                ('module', '=', module),
                ], order=[])
        for translation in module_translations:
            key = translation.unique_key
            if not key:
                raise ValueError('Unknown translation type: %s' %
                    translation.type)
            key2ids.setdefault(key, []).append(translation.id)
            if (
                len(module_translations) <=
                Transaction().context.get('_record_cache_size')
            ):
                id2translation[translation.id] = translation

        def override_translation(ressource_id, new_translation):
            res_id_module, res_id = ressource_id.split('.')
            if res_id:
                model_data, = ModelData.search([
                        ('module', '=', res_id_module),
                        ('fs_id', '=', res_id),
                        ])
                res_id = model_data.db_id
            else:
                res_id = -1
            with Transaction().set_user(0), \
                    Transaction().set_context(module=res_id_module):
                domain = [
                    ('name', '=', new_translation.name),
                    ('res_id', '=', res_id),
                    ('lang', '=', new_translation.lang),
                    ('type', '=', new_translation.type),
                    ('module', '=', res_id_module),
                    ]
                # Begin nereid changes
                if new_translation.type in ('odt', 'view', 'wizard_button',
                        'selection', 'error', 'nereid', 'nereid_template',
                        'wtforms'):
                        # End nereid changes
                    domain.append(('src', '=', new_translation.src))
                translation, = cls.search(domain)
                if translation.value != new_translation.value:
                    translation.value = new_translation.value
                    translation.overriding_module = module
                    translation.fuzzy = new_translation.fuzzy
                    translation.save()

        # Make a first loop to retreive translation ids in the right order to
        # get better read locality and a full usage of the cache.
        translation_ids = []
        record_cache_size = Transaction().context.get('_record_cache_size')
        if len(module_translations) <= record_cache_size:
            processes = (True,)
        else:
            processes = (False, True)
        for processing in processes:
            if processing and len(module_translations) > record_cache_size:
                id2translation = dict((t.id, t)
                    for t in cls.browse(translation_ids))
            for entry in pofile:
                translation, res_id = cls.from_poentry(entry)
                translation.lang = lang
                translation.module = module
                noupdate = False

                if '.' in res_id:
                    override_translation(res_id, translation)
                    continue

                model = translation.name.split(',')[0]
                if (model in fs_id2prop and
                        res_id in fs_id2prop[model]):
                    res_id, noupdate = fs_id2prop[model][res_id]

                if res_id:
                    try:
                        res_id = int(res_id)
                    except ValueError:
                        res_id = None
                if not res_id:
                    res_id = -1

                translation.res_id = res_id
                key = translation.unique_key
                if not key:
                    raise ValueError('Unknow translation type: %s' %
                        translation.type)
                ids = key2ids.get(key, [])

                if not processing:
                    translation_ids.extend(ids)
                    continue

                if not ids:
                    to_create.append(translation._save_values)
                else:
                    to_write = []
                    for translation_id in ids:
                        old_translation = id2translation[translation_id]
                        if (old_translation.value != translation.value or
                                old_translation.fuzzy !=
                                translation.fuzzy):
                            to_write.append(old_translation)
                    with Transaction().set_user(0), \
                            Transaction().set_context(module=module):
                        if to_write and not noupdate:
                            cls.write(to_write, {
                                    'value': translation.value,
                                    'fuzzy': translation.fuzzy,
                                    })
                        translations |= set(cls.browse(ids))

        if to_create:
            with Transaction().set_user(0), \
                    Transaction().set_context(module=module):
                translations |= set(cls.create(to_create))

        if translations:
            all_translations = set(cls.search([
                        ('module', '=', module),
                        ('lang', '=', lang),
                        ]))
            translations_to_delete = all_translations - translations
            cls.delete(list(translations_to_delete))
        return len(translations)

    @classmethod
    def translation_export(cls, lang, module):
        """
        Override the entire method: upstream code needs refactoring to allow
        for customization.
        Based on trytond version: 3.2.4
        """
        pool = Pool()
        ModelData = pool.get('ir.model.data')
        Config = pool.get('ir.configuration')

        models_data = ModelData.search([
            ('module', '=', module),
        ])
        db_id2fs_id = {}
        for model_data in models_data:
            db_id2fs_id.setdefault(model_data.model, {})
            db_id2fs_id[model_data.model][model_data.db_id] = model_data.fs_id
            for extra_model in cls.extra_model_data(model_data):
                db_id2fs_id.setdefault(extra_model, {})
                db_id2fs_id[extra_model][model_data.db_id] = model_data.fs_id

        pofile = TrytonPOFile(wrapwidth=78)
        pofile.metadata = {
            'Content-Type': 'text/plain; charset=utf-8',
        }

        with Transaction().set_context(language=Config.get_language()):
            translations = cls.search([
                ('lang', '=', lang),
                ('module', '=', module),
            ], order=[])
        for translation in translations:
            if (translation.overriding_module and
                    translation.overriding_module != module):
                cls.raise_user_error('translation_overridden', {
                    'name': translation.name,
                    'overriding_module': translation.overriding_module,
                })
            flags = [] if not translation.fuzzy else ['fuzzy']
            trans_ctxt = '%(type)s:%(name)s:' % {
                'type': translation.type,
                'name': translation.name,
            }
            res_id = translation.res_id

            # Begin nereid changes
            # don't export nereid items with res_id == -1, because there
            # is definitely something wrong with them (messages that weren't
            # updated, but just imported)
            if res_id == -1 and translation.type in _nereid_types:
                continue
            # append res_id generally for nereid items
            if res_id >= 0:
                if translation.type not in _nereid_types:
                    model, _ = translation.name.split(',')
                    if model in db_id2fs_id:
                        res_id = db_id2fs_id[model].get(res_id)
                    else:
                        continue
                trans_ctxt += '%s' % res_id
            # End nereid changes

            entry = polib.POEntry(
                msgid=(translation.src or ''),
                msgstr=(translation.value or ''),
                msgctxt=trans_ctxt,
                flags=flags
            )
            pofile.append(entry)

        if pofile:
            pofile.sort()
            return unicode(pofile).encode('utf-8')
        else:
            return

    # Begin nereid changes
    _nereid_translation_cache = Cache(
        'ir.translation', size_limit=10240, context=False
    # End nereid changes
    )

    @classmethod
    def get_translation_4_nereid(cls, module, ttype, lang, source):
        "Return translation for source"
        ttype = unicode(ttype)
        lang = unicode(lang)
        source = unicode(source)

        cache_key = (lang, ttype, source, module)

        trans = cls._nereid_translation_cache.get(cache_key, -1)
        if trans != -1:
            return trans

        cursor = Transaction().connection.cursor()
        table = cls.__table__()
        where = (
            (table.lang == lang) &
            (table.type == ttype) &
            (table.value != '') &
            (table.value != None) &
            (table.fuzzy == False) &
            (table.src == source)
        )
        if module is not None:
            where &= (table.module == module)

        cursor.execute(*table.select(table.value, where=where))
        res = cursor.fetchone()
        if res:
            cls._nereid_translation_cache.set(cache_key, res[0])
            return res[0]
        else:
            cls._nereid_translation_cache.set(cache_key, False)
            return None

    @classmethod
    def delete(cls, translations):
        cls._nereid_translation_cache.clear()
        return super(Translation, cls).delete(translations)

    @classmethod
    def create(cls, vlist):
        cls._nereid_translation_cache.clear()
        return super(Translation, cls).create(vlist)

    @classmethod
    def write(cls, translations, values):
        cls._nereid_translation_cache.clear()
        return super(Translation, cls).write(translations, values)


class TranslationSet:
    __name__ = "ir.translation.set"

    def transition_set_(self):
        state = super(TranslationSet, self).transition_set_()
        self.set_nereid_template()
        self.set_wtforms()
        self.set_nereid()
        return state

    @classmethod
    def _get_nereid_template_extract_options(cls):
        """
        a dictionary of additional options that can be passed on to
        `jinja2.ext.babel_extract`.
        """
        return {
            'extensions': ','.join([
                'jinja2.ext.i18n',
                'nereid.templating.FragmentCacheExtension'
            ]),
        }

    @classmethod
    def _get_installed_module_directories(cls):
        """
        A generator that yields tuples of the format (module_name, directory)
        for every installed module in the current database
        """
        from trytond.modules import create_graph, get_module_list, \
            MODULES_PATH, EGG_MODULES

        IrModule = Pool().get('ir.module')

        packages = list(create_graph(get_module_list())[0])[::-1]
        installed_module_list = map(
            lambda module: module.name,
            IrModule.search([('state', '=', 'installed')])
        )

        for package in packages:
            if package.name not in installed_module_list:
                # this package is not installed as a module in this
                # database and hence the tranlation is not relevant
                continue
            if package.name in EGG_MODULES:
                # trytond.tools has a good helper which allows resources to
                # be loaded from the installed site packages. Just use it
                # to load the tryton.cfg file which is guaranteed to exist
                # and from it lookup the directory. From here, its just
                # another searchpath for the loader.
                f = file_open(os.path.join(package.name, 'tryton.cfg'))
                module_dir = os.path.dirname(f.name)
            else:
                module_dir = os.path.join(MODULES_PATH, package.name)

            yield package.name, module_dir

    @classmethod
    def _get_nereid_template_messages(cls):
        """
        Extract localizable strings from the templates of installed modules.

        For every string found this function yields a
        `(module, template, lineno, function, message)` tuple, where:

        * module is the name of the module in which the template is found
        * template is the name of the template in which message was found
        * lineno is the number of the line on which the string was found,
        * function is the name of the gettext function used (if the string
          was extracted from embedded Python code), and
        * message is the string itself (a unicode object, or a tuple of
          unicode objects for functions with multiple string arguments).
        * comments List of Translation comments if any. Comments in the code
          should have a prefix `trans:`. Example::

              {{ _(Welcome) }} {# trans: In the top banner #}
        """
        extract_options = cls._get_nereid_template_extract_options()
        logger = logging.getLogger('nereid.translation')

        for module, directory in cls._get_installed_module_directories():
            template_dir = os.path.join(directory, 'templates')
            if not os.path.isdir(template_dir):
                # The template directory does not exist. Just continue
                continue

            logger.info(
                'Found template directory for module %s at %s' % (
                    module, template_dir
                )
            )
            # now that there is a template directory, load the templates
            # using a simple filesystem loader and load all the
            # translations from it.
            loader = FileSystemLoader(template_dir)
            env = Environment(loader=loader)
            extensions = '.html,.jinja'
            for template in env.list_templates(extensions=extensions):
                logger.info('Loading from: %s:%s' % (module, template))
                file_obj = open(loader.get_source({}, template)[1])
                for message_tuple in babel_extract(
                        file_obj, GETTEXT_FUNCTIONS,
                        ['trans:'], extract_options):
                    yield (module, template) + message_tuple

    @staticmethod
    def _get_nereid_template_messages_from_file(self, template_dir, template):
        """
        Same generator as _get_nereid_template_messages, but for specific files.
        """
        extract_options = self._get_nereid_template_extract_options()
        loader = FileSystemLoader(template_dir)
        file_obj = open(loader.get_source({}, template)[1])
        for message_tuple in babel_extract(
                file_obj, GETTEXT_FUNCTIONS,
                ['trans:'], extract_options):
            yield (template,) + message_tuple

    def set_nereid_template(self):
        """
        Loads all nereid templates translatable strings into the database. The
        templates loaded are only the ones which are bundled with the tryton
        modules and available in the site packages.
        """
        pool = Pool()
        Translation = pool.get('ir.translation')
        to_create = []
        for module, template, lineno, function, messages, comments in \
                self._get_nereid_template_messages():

            if isinstance(messages, basestring):
                # messages could be a tuple if the function is ngettext
                # where the messages for singular and plural are given as
                # a tuple.
                #
                # So convert basestrings to tuples
                messages = (messages, )

            for message in messages:
                translations = Translation.search([
                    ('lang', '=', 'en_US'),
                    ('type', '=', 'nereid_template'),
                    ('name', '=', template),
                    ('src', '=', message),
                    ('module', '=', module),
                    ('res_id', '=', lineno),
                ], limit=1)
                if translations:
                    continue
                to_create.append({
                    'name': template,
                    'res_id': lineno,
                    'lang': 'en_US',
                    'src': message,
                    'type': 'nereid_template',
                    'module': module,
                    'comments': comments and '\n'.join(comments) or None,
                })
        if to_create:
            Translation.create(to_create)

    def set_wtforms(self):
        """
        There are some messages in WTForms which are provided by the framework,
        namely default validator messages and errors occuring during the
        processing (data coercion) stage. For example, in the case of the
        IntegerField, if someone entered a value which was not valid as
        an integer, then a message like “Not a valid integer value” would be
        displayed.
        """
        pool = Pool()
        Translation = pool.get('ir.translation')
        to_create = []
        for (filename, lineno, messages, comments, context) in \
                extract_from_dir(os.path.dirname(wtforms.__file__)):

            if isinstance(messages, basestring):
                # messages could be a tuple if the function is ngettext
                # where the messages for singular and plural are given as
                # a tuple.
                #
                # So convert basestrings to tuples
                messages = (messages, )

            for message in messages:
                translations = Translation.search([
                    ('lang', '=', 'en_US'),
                    ('type', '=', 'wtforms'),
                    ('name', '=', filename),
                    ('src', '=', message),
                    ('module', '=', 'nereid'),
                ], limit=1)
                if translations:
                    continue
                to_create.append({
                    'name': filename,
                    'res_id': lineno,
                    'lang': 'en_US',
                    'src': message,
                    'type': 'wtforms',
                    'module': 'nereid',
                    'comments': comments and '\n'.join(comments) or None,
                })
        if to_create:
            Translation.create(to_create)

    @staticmethod
    def _get_babel_messages_from_file(self, template):
        """
        Get babel messages from a specific file.
        """
        for (lineno, messages, _, _) in extract_from_file('python', template):
            if isinstance(messages, basestring):
                messages = (messages, )
            for message in messages:
                yield (template, lineno, message)

    def set_nereid(self):
        """
        There are messages within the tryton code used in flash messages,
        returned responses etc. This is spread over the codebase and this
        function extracts the translation strings from code of installed
        modules.
        """
        pool = Pool()
        Translation = pool.get('ir.translation')
        to_create = []

        for module, directory in self._get_installed_module_directories():
            # skip messages from test files
            if 'tests' in directory:
                continue
            for (filename, lineno, messages, comments, context) in \
                    extract_from_dir(directory,):

                if isinstance(messages, basestring):
                    # messages could be a tuple if the function is ngettext
                    # where the messages for singular and plural are given as
                    # a tuple.
                    #
                    # So convert basestrings to tuples
                    messages = (messages, )

                for message in messages:
                    translations = Translation.search([
                        ('lang', '=', 'en_US'),
                        ('type', '=', 'nereid'),
                        ('name', '=', filename),
                        ('src', '=', message),
                        ('module', '=', module),
                    ], limit=1)
                    if translations:
                        continue
                    to_create.append({
                        'name': filename,
                        'res_id': lineno,
                        'lang': 'en_US',
                        'src': message,
                        'type': 'nereid',
                        'module': module,
                        'comments': comments and '\n'.join(comments) or None,
                    })
        if to_create:
            Translation.create(to_create)


class TranslationUpdate:
    __name__ = "ir.translation.update"

    def do_update(self, action):
        pool = Pool()
        Translation = pool.get('ir.translation')

        cursor = Transaction().connection.cursor()
        lang = self.start.language.code
        translation = Translation.__table__()

        types = ['nereid_template', 'wtforms', 'nereid']
        columns = [
            translation.name.as_('name'),
            translation.res_id.as_('res_id'),
            translation.type.as_('type'),
            translation.src.as_('src'),
            translation.module.as_('module'),
            translation.comments.as_('comments'),
        ]
        cursor.execute(*(
            translation.select(
                *columns,
                where=(translation.lang == 'en_US') &
                translation.type.in_(types)) -
            translation.select(
                *columns,
                where=(translation.lang == lang) &
                translation.type.in_(types))
        ))
        to_create = []
        for row in cursor_dict(cursor):
            to_create.append({
                'name': row['name'],
                'res_id': row['res_id'],
                'lang': lang,
                'type': row['type'],
                'src': row['src'],
                'module': row['module'],
                'comments': row['comments'],
            })
        if to_create:
            with Transaction().set_user(0):
                Translation.create(to_create)
        return super(TranslationUpdate, self).do_update(action)


class TranslationClean(Wizard):
    "Clean translation"
    __name__ = 'ir.translation.clean'

    @staticmethod
    def _clean_nereid_template(translation):
        """
        Clean the template translations if the module is not installed, or if
        the template is not there.
        """
        TranslationSet = Pool().get('ir.translation.set', type='wizard')
        installed_modules = TranslationSet._get_installed_module_directories()

        # Clean if the module is not installed anymore
        for module, directory in installed_modules:
            if translation.module == module:
                break
        else:
            return True

        # Clean if the template directory does not exist
        template_dir = os.path.join(directory, 'templates')
        if not os.path.isdir(template_dir):
            return True

        # Clean if the template is not found
        loader = FileSystemLoader(template_dir)
        if translation.name not in loader.list_templates():
            return True

        # Clean if the translation has changed (avoid duplicates)
        # (translation has no equivalent in template)
        found = False
        for template, lineno, function, message, comments in \
            TranslationSet._get_nereid_template_messages_from_file(
                TranslationSet, template_dir, translation.name):
            if (template, lineno, message, comments and
                    '\n'.join(comments) or None) == \
                (translation.name, translation.res_id, translation.src,
                    translation.comments):
                found = True
                break
        if not found:
            return True

    @staticmethod
    def _clean_wtforms(translation):
        """
        Clean the translation if nereid is not installed
        """
        TranslationSet = Pool().get('ir.translation.set', type='wizard')
        installed_modules = TranslationSet._get_installed_module_directories()

        # Clean if the module is not installed anymore
        for module, directory in installed_modules:
            if translation.module == module:
                break
        else:
            return True

        # Clean if the translation has changed (avoid duplicates)
        # (translation has no equivalent in template)
        babel_file = os.path.join(os.path.dirname(wtforms.__file__),
            translation.name)
        if not os.path.exists(babel_file):
            return True
        found = False
        for template, lineno, message in \
            TranslationSet._get_babel_messages_from_file(TranslationSet,
                babel_file):
            if (lineno, message) == (translation.res_id, translation.src):
                found = True
                break
        if not found:
            return True

    @staticmethod
    def _clean_nereid(translation):
        """
        Remove the nereid translations if the module is not installed
        """
        TranslationSet = Pool().get('ir.translation.set', type='wizard')
        installed_modules = TranslationSet._get_installed_module_directories()

        # Clean if the module is not installed anymore
        for module, directory in installed_modules:
            if translation.module == module:
                break
        else:
            return True

        # Clean any messages from tests
        if 'tests' in translation.name.split('/'):
            return True

        # Clean if the translation has changed (avoid duplicates)
        # (translation has no equivalent in template)
        babel_file = os.path.join(directory, translation.name)
        if not os.path.exists(babel_file):
            return True
        found = False
        for template, lineno, message in \
            TranslationSet._get_babel_messages_from_file(TranslationSet,
                babel_file):
            if (lineno, message) == (translation.res_id, translation.src):
                found = True
                break
        if not found:
            return True
