import sentry_sdk
from symbolic import ProguardMapper

from sentry.lang.java.processing import deobfuscate_exception_value
from sentry.lang.java.utils import (
    deobfuscate_view_hierarchy,
    get_proguard_images,
    has_proguard_file,
)
from sentry.models import EventError, ProjectDebugFile
from sentry.plugins.base.v2 import Plugin2
from sentry.reprocessing import report_processing_issue
from sentry.stacktraces.processing import StacktraceProcessor


class JavaStacktraceProcessor(StacktraceProcessor):
    def __init__(self, *args, **kwargs):
        StacktraceProcessor.__init__(self, *args, **kwargs)

        self.images = get_proguard_images(self.data)
        self.available = len(self.images) > 0

    def handles_frame(self, frame, stacktrace_info):
        platform = frame.get("platform") or self.data.get("platform")
        return platform == "java" and self.available and "function" in frame and "module" in frame

    def preprocess_step(self, processing_task):
        if not self.available:
            return False

        with sentry_sdk.start_span(op="proguard.fetch_debug_files"):
            dif_paths = ProjectDebugFile.difcache.fetch_difs(
                self.project, self.images, features=["mapping"]
            )
            self.mapping_views = []

        for debug_id in self.images:
            error_type = None

            dif_path = dif_paths.get(debug_id)
            if dif_path is None:
                error_type = EventError.PROGUARD_MISSING_MAPPING
            else:
                with sentry_sdk.start_span(op="proguard.open"):
                    view = ProguardMapper.open(dif_path)
                    if not view.has_line_info:
                        error_type = EventError.PROGUARD_MISSING_LINENO
                    else:
                        self.mapping_views.append(view)

            if error_type is None:
                continue

            self.data.setdefault("_metrics", {})["flag.processing.error"] = True

            self.data.setdefault("errors", []).append(
                {"type": error_type, "mapping_uuid": debug_id}
            )

            report_processing_issue(
                self.data,
                scope="proguard",
                object="mapping:%s" % debug_id,
                type=error_type,
                data={"mapping_uuid": debug_id},
            )

        return True

    def process_exception(self, exception):
        ty = exception.get("type")
        mod = exception.get("module")
        if not ty or not mod:
            return False

        key = f"{mod}.{ty}"

        for view in self.mapping_views:
            mapped = view.remap_class(key)
            if mapped:
                new_module, new_cls = mapped.rsplit(".", 1)
                exception["module"] = new_module
                exception["type"] = new_cls
                return True

        return False

    def process_frame(self, processable_frame, processing_task):
        frame = processable_frame.frame
        raw_frame = dict(frame)

        # first, try to remap complete frames
        for view in self.mapping_views:
            mapped = view.remap_frame(frame["module"], frame["function"], frame.get("lineno") or 0)

            if len(mapped) > 0:
                new_frames = []
                bottom_class = mapped[-1].class_name

                # sentry expects stack traces in reverse order
                for new_frame in reversed(mapped):
                    frame = dict(raw_frame)
                    frame["module"] = new_frame.class_name
                    frame["function"] = new_frame.method
                    frame["lineno"] = new_frame.line

                    # clear the filename for all *foreign* classes
                    if frame["module"] != bottom_class:
                        frame.pop("filename", None)
                        frame.pop("abs_path", None)

                    new_frames.append(frame)

                return new_frames, [raw_frame], []

        # second, if that is not possible, try to re-map only the class-name
        for view in self.mapping_views:
            mapped = view.remap_class(frame["module"])

            if mapped:
                new_frame = dict(raw_frame)
                new_frame["module"] = mapped
                return [new_frame], [raw_frame], []

        return


class JavaPlugin(Plugin2):
    can_disable = False

    def can_configure_for_project(self, project, **kwargs):
        return False

    def get_stacktrace_processors(self, data, stacktrace_infos, platforms, **kwargs):
        if "java" in platforms:
            return [JavaStacktraceProcessor]

    def get_event_preprocessors(self, data):
        if has_proguard_file(data):
            return [deobfuscate_exception_value, deobfuscate_view_hierarchy]
