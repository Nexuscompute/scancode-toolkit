#
# Copyright (c) nexB Inc. and others. All rights reserved.
# ScanCode is a trademark of nexB Inc.
# SPDX-License-Identifier: Apache-2.0
# See http://www.apache.org/licenses/LICENSE-2.0 for the license text.
# See https://github.com/nexB/scancode-toolkit for support or download.
# See https://aboutcode.org for more information about nexB OSS projects.
#


import logging
import os
from functools import partial

import attr
from commoncode.cliutils import MISC_GROUP
from commoncode.cliutils import PluggableCommandLineOption
from commoncode.cliutils import SCAN_GROUP
from commoncode.cliutils import SCAN_OPTIONS_GROUP
from plugincode.scan import ScanPlugin
from plugincode.scan import scan_impl
from licensedcode.cache import build_spdx_license_expression, get_cache
from licensedcode.detection import DetectionCategory
from licensedcode.detection import find_referenced_resource
from licensedcode.detection import get_detected_license_expression
from licensedcode.detection import get_matches_from_detection_mappings
from licensedcode.detection import get_referenced_filenames
from licensedcode.detection import SCANCODE_LICENSEDB_URL
from packagedcode.utils import combine_expressions
from scancode.api import SCANCODE_LICENSEDB_URL

TRACE = os.environ.get('SCANCODE_DEBUG_PLUGIN_LICENSE', False)


def logger_debug(*args):
    pass


logger = logging.getLogger(__name__)


if TRACE:
    import sys

    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(' '.join(isinstance(a, str) and a or repr(a) for a in args))


def reindex_licenses(ctx, param, value):
    """
    Rebuild and cache the license index
    """
    if not value or ctx.resilient_parsing:
        return

    # TODO: check for temp file configuration and use that for the cache!!!
    import click

    from licensedcode.cache import get_index
    click.echo('Rebuilding the license index...')
    get_index(force=True)
    click.echo('Done.')
    ctx.exit(0)


def reindex_licenses_all_languages(ctx, param, value):
    """
    EXPERIMENTAL: Rebuild and cache the license index including all languages
    and not only English.
    """
    if not value or ctx.resilient_parsing:
        return

    # TODO: check for temp file configuration and use that for the cache!!!
    import click

    from licensedcode.cache import get_index
    click.echo('Rebuilding the license index for all languages...')
    get_index(force=True, index_all_languages=True)
    click.echo('Done.')
    ctx.exit(0)


@scan_impl
class LicenseScanner(ScanPlugin):
    """
    Scan a Resource for licenses.
    """

    resource_attributes = dict([
        ('detected_license_expression', attr.ib(default=None)),
        ('detected_license_expression_spdx', attr.ib(default=None)),
        ('license_detections', attr.ib(default=attr.Factory(list))),
        ('license_clues', attr.ib(default=attr.Factory(list))),
        ('percentage_of_license_text', attr.ib(default=0)),
    ])

    sort_order = 2

    options = [
        PluggableCommandLineOption(('-l', '--license'),
            is_flag=True,
            help='Scan <input> for licenses.',
            help_group=SCAN_GROUP,
            sort_order=10,
        ),

        PluggableCommandLineOption(('--license-score',),
            type=int, default=0, show_default=True,
            required_options=['license'],
            help='Do not return license matches with a score lower than this score. '
                 'A number between 0 and 100.',
            help_group=SCAN_OPTIONS_GROUP,
        ),

        PluggableCommandLineOption(('--license-text',),
            is_flag=True,
            required_options=['license'],
            help='Include the detected licenses matched text.',
            help_group=SCAN_OPTIONS_GROUP,
        ),

        PluggableCommandLineOption(('--license-text-diagnostics',),
            is_flag=True,
            required_options=['license_text'],
            help='In the matched license text, include diagnostic highlights '
                 'surrounding with square brackets [] words that are not matched.',
            help_group=SCAN_OPTIONS_GROUP,
        ),

        PluggableCommandLineOption(('--license-url-template',),
            default=SCANCODE_LICENSEDB_URL, show_default=True,
            required_options=['license'],
            help='Set the template URL used for the license reference URLs. '
                 'Curly braces ({}) are replaced by the license key.',
            help_group=SCAN_OPTIONS_GROUP,
        ),

        PluggableCommandLineOption(
            ('--reindex-licenses',),
            is_flag=True, is_eager=True,
            callback=reindex_licenses,
            help='Rebuild the license index and exit.',
            help_group=MISC_GROUP,
        ),

        PluggableCommandLineOption(
            ('--reindex-licenses-for-all-languages',),
            is_flag=True, is_eager=True,
            callback=reindex_licenses_all_languages,
            help='[EXPERIMENTAL] Rebuild the license index including texts all '
                 'languages (and not only English) and exit.',
            help_group=MISC_GROUP,
        )

    ]

    def is_enabled(self, license, **kwargs):  # NOQA
        return license

    def setup(self, **kwargs):
        """
        This is a cache warmup such that child process inherit from the
        loaded index.
        """
        from licensedcode.cache import populate_cache
        populate_cache()

    def get_scanner(
        self,
        license_score=0,
        license_text=False,
        license_text_diagnostics=False,
        license_url_template=SCANCODE_LICENSEDB_URL,
        **kwargs
    ):

        from scancode.api import get_licenses
        return partial(get_licenses,
            min_score=license_score,
            include_text=license_text,
            license_text_diagnostics=license_text_diagnostics,
            license_url_template=license_url_template,
        )

    def process_codebase(self, codebase, **kwargs):
        """
        Post process the codebase to further detect unknown licenses and follow
        license references to other files.

        This is an EXPERIMENTAL feature for now.
        """
        if codebase.has_single_resource:
            return

        for resource in codebase.walk(topdown=False):
            # follow license references to other files
            if TRACE:
                license_expressions_before = list(resource.license_expressions)

            modified = add_referenced_license_matches_for_detections(resource, codebase)

            if TRACE and modified:
                license_expressions_after = list(resource.license_expressions)
                logger_debug(
                    f'add_referenced_filenames_matches: Modfied:',
                    f'{resource.path} with license_expressions:\n'
                    f'before: {license_expressions_before}\n'
                    f'after : {license_expressions_after}'
                )


def add_referenced_license_matches_for_detections(resource, codebase):
    """
    Return an updated ``resource`` saving it in place, after adding new license
    matches (licenses and license_expressions) following their Rule
    ``referenced_filenames`` if any. Return None if ``resource`` is not a file
    Resource or was not updated.
    """
    if not resource.is_file:
        return

    license_detections = resource.license_detections
    if not license_detections:
        return

    modified = False

    for detection in license_detections:
        detection_modified = False
        matches = detection["matches"]
        referenced_filenames = get_referenced_filenames(matches)
        if not referenced_filenames:
            continue 
        
        for referenced_filename in referenced_filenames:
            referenced_resource = find_referenced_resource(
                referenced_filename=referenced_filename,
                resource=resource,
                codebase=codebase,
            )

            if referenced_resource and referenced_resource.license_detections:
                modified = True
                detection_modified = True
                matches.extend(
                    get_matches_from_detection_mappings(
                        license_detections=referenced_resource.license_detections
                    )
                )

        if not detection_modified:
            continue

        reasons, license_expression = get_detected_license_expression(
            matches=matches,
            analysis=DetectionCategory.UNKNOWN_FILE_REFERENCE_LOCAL.value,
            post_scan=True,
        )
        detection["license_expression"] = str(license_expression)
        detection["detection_rules"] = reasons

    if modified:
        license_expressions = [
            detection["license_expression"]
            for detection in resource.license_detections
        ]
        resource.detected_license_expression = combine_expressions(
            expressions=license_expressions,
            relation='AND',
            unique=True,
        )

        resource.detected_license_expression_spdx = str(build_spdx_license_expression(
            license_expression=resource.detected_license_expression,
            licensing=get_cache().licensing,
        ))

        codebase.save_resource(resource)
        return resource
