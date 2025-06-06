"""Defines the templaters."""
import logging
import os.path
import pkgutil
import importlib
import sys
from functools import reduce
from typing import Callable, Dict, Generator, List, Optional, Tuple

import jinja2.nodes
from jinja2 import (
    Environment,
    FileSystemLoader,
    TemplateError,
    TemplateSyntaxError,
    meta,
)
from jinja2.environment import Template
from jinja2.exceptions import TemplateNotFound, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from sqlfluff.core.config import FluffConfig
from sqlfluff.core.errors import SQLBaseError, SQLTemplaterError
from sqlfluff.core.templaters.base import (
    RawFileSlice,
    TemplatedFile,
    TemplatedFileSlice,
    large_file_check,
)
from sqlfluff.core.templaters.python import PythonTemplater
from sqlfluff.core.templaters.slicers.tracer import JinjaAnalyzer

# Instantiate the templater logger
templater_logger = logging.getLogger("sqlfluff.templater")


class JinjaTemplater(PythonTemplater):
    """A templater using the jinja2 library.

    See: https://jinja.palletsprojects.com/
    """

    name = "jinja"

    class Libraries:
        """Mock namespace for user-defined Jinja library."""

        pass

    @staticmethod
    def _extract_macros_from_template(template, env, ctx):
        """Take a template string and extract any macros from it.

        Lovingly inspired by http://codyaray.com/2015/05/auto-load-jinja2-macros
        """
        from jinja2.runtime import Macro  # noqa

        # Iterate through keys exported from the loaded template string
        context = {}
        macro_template = env.from_string(template, globals=ctx)
        # This is kind of low level and hacky but it works
        try:
            for k in macro_template.module.__dict__:
                attr = getattr(macro_template.module, k)
                # Is it a macro? If so install it at the name of the macro
                if isinstance(attr, Macro):
                    context[k] = attr
        except UndefinedError:
            # This occurs if any file in the macro path references an
            # undefined Jinja variable. It's safe to ignore this. Any
            # meaningful issues will surface later at linting time.
            pass
        # Return the context
        return context

    @classmethod
    def _extract_macros_from_path(cls, path: List[str], env: Environment, ctx: Dict):
        """Take a path and extract macros from it."""
        macro_ctx = {}
        for path_entry in path:
            # Does it exist? It should as this check was done on config load.
            if not os.path.exists(path_entry):
                raise ValueError(f"Path does not exist: {path_entry}")

            if os.path.isfile(path_entry):
                # It's a file. Extract macros from it.
                with open(path_entry) as opened_file:
                    template = opened_file.read()
                # Update the context with macros from the file.
                try:
                    macro_ctx.update(
                        cls._extract_macros_from_template(template, env=env, ctx=ctx)
                    )
                except TemplateSyntaxError as err:
                    raise SQLTemplaterError(
                        f"Error in Jinja macro file {os.path.relpath(path_entry)}: "
                        f"{err.message}",
                        line_no=err.lineno,
                        line_pos=1,
                    ) from err
            else:
                # It's a directory. Iterate through files in it and extract from them.
                for dirpath, _, files in os.walk(path_entry):
                    for fname in files:
                        if fname.endswith(".sql"):
                            macro_ctx.update(
                                cls._extract_macros_from_path(
                                    [os.path.join(dirpath, fname)], env=env, ctx=ctx
                                )
                            )
        return macro_ctx

    def _extract_macros_from_config(self, config, env, ctx):
        """Take a config and load any macros from it."""
        if config:
            # This is now a nested section
            loaded_context = (
                config.get_section((self.templater_selector, self.name, "macros")) or {}
            )
        else:  # pragma: no cover TODO?
            loaded_context = {}

        # Iterate to load macros
        macro_ctx = {}
        for value in loaded_context.values():
            macro_ctx.update(
                self._extract_macros_from_template(value, env=env, ctx=ctx)
            )
        return macro_ctx

    def _extract_libraries_from_config(self, config):
        library_path = config.get_section(
            (self.templater_selector, self.name, "library_path")
        )
        if not library_path:
            return {}

        libraries = JinjaTemplater.Libraries()

        # If library_path has __init__.py we parse it as one module, else we parse it
        # a set of modules
        is_library_module = os.path.exists(os.path.join(library_path, "__init__.py"))
        library_module_name = os.path.basename(library_path)

        # Need to go one level up to parse as a module correctly
        walk_path = (
            os.path.join(library_path, "..") if is_library_module else library_path
        )

        for module_finder, module_name, _ in pkgutil.walk_packages([walk_path]):
            # skip other modules that can be near module_dir
            if is_library_module and not module_name.startswith(library_module_name):
                continue

            # import_module is deprecated as of python 3.4. This follows roughly
            # the guidance of the python docs:
            # https://docs.python.org/3/library/importlib.html#approximating-importlib-import-module
            spec = module_finder.find_spec(module_name)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            if "." in module_name:  # nested modules have `.` in module_name
                *module_path, last_module_name = module_name.split(".")
                # find parent module recursively
                parent_module = reduce(
                    lambda res, path_part: getattr(res, path_part),
                    module_path,
                    libraries,
                )

                # set attribute on module object to make jinja working correctly
                setattr(parent_module, last_module_name, module)
            else:
                # set attr on `libraries` obj to make it work in jinja nicely
                setattr(libraries, module_name, module)

        if is_library_module:
            # when library is module we have one more root module in hierarchy and we
            # remove it
            libraries = getattr(libraries, library_module_name)

        # remove magic methods from result
        return {k: v for k, v in libraries.__dict__.items() if not k.startswith("__")}

    @staticmethod
    def _generate_dbt_builtins():
        """Generate the dbt builtins which are injected in the context."""
        # This feels a bit wrong defining these here, they should probably
        # be configurable somewhere sensible. But for now they're not.
        # TODO: Come up with a better solution.

        class ThisEmulator:
            """A class which emulates the `this` class from dbt."""

            name = "this_model"
            schema = "this_schema"
            database = "this_database"

            def __str__(self):  # pragma: no cover TODO?
                return self.name

        dbt_builtins = {
            "set_sql_header": lambda *args, **kwargs: "",
            "ref": lambda model_ref: model_ref,
            "source": lambda source_name, table: f"{source_name}_{table}",
            "config": lambda **kwargs: "",
            "var": lambda variable, default="": "item",
            # `is_incremental()` renders as True, always in this case.
            # TODO: This means we'll never parse other parts of the query,
            # that are only reachable when `is_incremental()` returns False.
            # We should try to find a solution to that. Perhaps forcing the file
            # to be parsed TWICE if it uses this variable.
            "is_incremental": lambda: True,
            "this": ThisEmulator(),
        }
        return dbt_builtins

    @classmethod
    def _crawl_tree(
        cls, tree, variable_names, raw
    ) -> Generator[SQLTemplaterError, None, None]:
        """Crawl the tree looking for occurrences of the undeclared values."""
        # First iterate through children
        for elem in tree.iter_child_nodes():
            yield from cls._crawl_tree(elem, variable_names, raw)
        # Then assess self
        if (
            isinstance(tree, jinja2.nodes.Name)
            and getattr(tree, "name") in variable_names
        ):
            line_no: int = getattr(tree, "lineno")
            tree_name: str = getattr(tree, "name")
            line = raw.split("\n")[line_no - 1]
            pos = line.index(tree_name) + 1
            yield SQLTemplaterError(
                f"Undefined jinja template variable: {tree_name!r}",
                line_no=line_no,
                line_pos=pos,
            )

    def _get_jinja_env(self, config=None):
        """Get a properly configured jinja environment."""
        # We explicitly want to preserve newlines.
        macros_path = self._get_macros_path(config)
        ignore_templating = config and "templating" in config.get("ignore")
        if ignore_templating:

            class SafeFileSystemLoader(FileSystemLoader):
                def get_source(self, environment, name, *args, **kwargs):
                    try:
                        if not isinstance(name, DummyUndefined):
                            return super().get_source(
                                environment, name, *args, **kwargs
                            )
                        raise TemplateNotFound(str(name))
                    except TemplateNotFound:
                        # When ignore=templating is set, treat missing files
                        # or attempts to load an "Undefined" file as the first
                        # 'base' part of the name / filename rather than failing.
                        templater_logger.debug(
                            "Providing dummy contents for Jinja macro file: %s", name
                        )
                        value = os.path.splitext(os.path.basename(str(name)))[0]
                        return value, f"{value}.sql", lambda: False

            loader = SafeFileSystemLoader(macros_path or [])
        else:
            loader = FileSystemLoader(macros_path) if macros_path else None
        extensions = ["jinja2.ext.do"]
        if self._apply_dbt_builtins(config):
            from dbt.clients.jinja import TestExtension, MaterializationExtension
            extensions.append(TestExtension)
            extensions.append(MaterializationExtension)

        return SandboxedEnvironment(
            keep_trailing_newline=True,
            # The do extension allows the "do" directive
            autoescape=False,
            extensions=extensions,
            loader=loader,
        )

    def _get_macros_path(self, config: FluffConfig) -> Optional[List[str]]:
        if config:
            macros_path = config.get_section(
                (self.templater_selector, self.name, "load_macros_from_path")
            )
            if macros_path:
                result = [s.strip() for s in macros_path.split(",") if s.strip()]
                if result:
                    return result
        return None

    def _apply_dbt_builtins(self, config: FluffConfig) -> bool:
        if config:
            return config.get_section(
                (self.templater_selector, self.name, "apply_dbt_builtins")
            )
        return False

    def get_context(self, fname=None, config=None, **kw) -> Dict:
        """Get the templating context from the config."""
        # Load the context
        env = kw.pop("env")
        live_context = super().get_context(fname=fname, config=config)
        # Apply dbt builtin functions if we're allowed.
        if config:
            # first make libraries available in the context
            # so they can be used by the macros too
            live_context.update(self._extract_libraries_from_config(config=config))

            if self._apply_dbt_builtins(config):
                # This feels a bit wrong defining these here, they should probably
                # be configurable somewhere sensible. But for now they're not.
                # TODO: Come up with a better solution.
                dbt_builtins = self._generate_dbt_builtins()
                for name in dbt_builtins:
                    # Only apply if it hasn't already been set at this stage.
                    if name not in live_context:
                        live_context[name] = dbt_builtins[name]

        # Load macros from path (if applicable)
        if config:
            macros_path = self._get_macros_path(config)
            if macros_path:
                live_context.update(
                    self._extract_macros_from_path(
                        macros_path, env=env, ctx=live_context
                    )
                )
                # parse twice like how dbt does it, this will support macro within macro ala dbt.
                live_context.update(
                    self._extract_macros_from_path(
                        macros_path, env=env, ctx=live_context
                    )
                )

            # Load config macros, these will take precedence over macros from the path
            live_context.update(
                self._extract_macros_from_config(
                    config=config, env=env, ctx=live_context
                )
            )

        return live_context

    def template_builder(
        self, fname=None, config=None
    ) -> Tuple[Environment, dict, Callable[[str], Template]]:
        """Builds and returns objects needed to create and run templates."""
        # Load the context
        env = self._get_jinja_env(config)
        live_context = self.get_context(fname=fname, config=config, env=env)

        def make_template(in_str):
            """Used by JinjaTracer to instantiate templates.

            This function is a closure capturing internal state from process().
            Note that creating templates involves quite a bit of state known to
            _this_ function but not to JinjaTracer.

            https://www.programiz.com/python-programming/closure
            """
            return env.from_string(in_str, globals=live_context)

        return env, live_context, make_template

    @large_file_check
    def process(
        self, *, in_str: str, fname: str, config=None, formatter=None
    ) -> Tuple[Optional[TemplatedFile], list]:
        """Process a string and return the new string.

        Note that the arguments are enforced as keywords
        because Templaters can have differences in their
        `process` method signature.
        A Templater that only supports reading from a file
        would need the following signature:
            process(*, fname, in_str=None, config=None)
        (arguments are swapped)

        Args:
            in_str (:obj:`str`): The input string.
            fname (:obj:`str`, optional): The filename of this string. This is
                mostly for loading config files at runtime.
            config (:obj:`FluffConfig`): A specific config to use for this
                templating operation. Only necessary for some templaters.
            formatter (:obj:`CallbackFormatter`): Optional object for output.

        """
        if not config:  # pragma: no cover
            raise ValueError(
                "For the jinja templater, the `process()` method requires a config "
                "object."
            )

        try:
            env, live_context, make_template = self.template_builder(
                fname=fname, config=config
            )
        except SQLTemplaterError as err:
            return None, [err]

        # Load the template, passing the global context.
        try:
            template = make_template(in_str)
        except TemplateSyntaxError as err:
            # Something in the template didn't parse, return the original
            # and a violation around what happened.
            return (
                TemplatedFile(source_str=in_str, fname=fname),
                [
                    SQLTemplaterError(
                        f"Failure to parse jinja template: {err}.",
                        line_no=err.lineno,
                    )
                ],
            )

        violations: List[SQLBaseError] = []

        # Attempt to identify any undeclared variables. The majority
        # will be found during the _crawl_tree step rather than this
        # first Exception which serves only to catch catastrophic errors.
        try:
            syntax_tree = env.parse(in_str)
            potentially_undefined_variables = meta.find_undeclared_variables(
                syntax_tree
            )
        except Exception as err:  # pragma: no cover
            # TODO: Add a url here so people can get more help.
            raise SQLTemplaterError(f"Failure in identifying Jinja variables: {err}.")

        undefined_variables = set()

        class UndefinedRecorder:
            """Similar to jinja2.StrictUndefined, but remembers, not fails."""

            # Tell Jinja this object is safe to call and does not alter data.
            # https://jinja.palletsprojects.com/en/2.9.x/sandbox/#jinja2.sandbox.SandboxedEnvironment.is_safe_callable
            unsafe_callable = False
            # https://jinja.palletsprojects.com/en/3.0.x/sandbox/#jinja2.sandbox.SandboxedEnvironment.is_safe_callable
            alters_data = False

            @classmethod
            def create(cls, name):
                return UndefinedRecorder(name=name)

            def __init__(self, name):
                self.name = name

            def __str__(self):
                """Treat undefined vars as empty, but remember for later."""
                undefined_variables.add(self.name)
                return ""

            def __getattr__(self, item):
                undefined_variables.add(self.name)
                return UndefinedRecorder(f"{self.name}.{item}")

            def __call__(self, *args, **kwargs):
                return UndefinedRecorder(f"{self.name}()")

        Undefined = (
            UndefinedRecorder
            if "templating" not in config.get("ignore")
            else DummyUndefined
        )
        for val in potentially_undefined_variables:
            if val not in live_context:
                if val.endswith('_tbl'):
                    live_context[val] = 'some_tbl'
                else:
                    live_context[val] = Undefined.create(val)  # type: ignore

        try:
            # NB: Passing no context. Everything is loaded when the template is loaded.
            out_str = template.render(**live_context)
            # Slice the file once rendered.
            raw_sliced, sliced_file, out_str = self.slice_file(
                in_str,
                out_str,
                config=config,
                make_template=make_template,
            )
            if undefined_variables:
                # Lets go through and find out where they are:
                for template_err_val in self._crawl_tree(
                    syntax_tree, undefined_variables, in_str
                ):
                    violations.append(template_err_val)
            return (
                TemplatedFile(
                    source_str=in_str,
                    templated_str=out_str,
                    fname=fname,
                    sliced_file=sliced_file,
                    raw_sliced=raw_sliced,
                ),
                violations,
            )
        except (TemplateError, TypeError) as err:
            templater_logger.info("Unrecoverable Jinja Error: %s", err, exc_info=True)
            template_err: SQLBaseError = SQLTemplaterError(
                (
                    "Unrecoverable failure in Jinja templating: {}. Have you "
                    "configured your variables? "
                    "https://docs.sqlfluff.com/en/latest/configuration.html"
                ).format(err),
                # We don't have actual line number information, but specify
                # line 1 so users can ignore with "noqa" if they want. (The
                # default is line 0, which can't be ignored because it's not
                # a valid line number.)
                line_no=1,
                line_pos=1,
            )
            violations.append(template_err)
            return None, violations

    def slice_file(
        self, raw_str: str, templated_str: str, config=None, **kwargs
    ) -> Tuple[List[RawFileSlice], List[TemplatedFileSlice], str]:
        """Slice the file to determine regions where we can fix."""
        # The JinjaTracer slicing algorithm is more robust, but it requires
        # us to create and render a second template (not raw_str) and is only
        # enabled if the caller passes a make_template() function.
        make_template = kwargs.pop("make_template", None)
        if make_template is None:
            # make_template() was not provided. Use the base class
            # implementation instead.
            return super().slice_file(
                raw_str, templated_str, config, **kwargs
            )  # pragma: no cover

        templater_logger.info("Slicing File Template")
        templater_logger.debug("    Raw String: %r", raw_str)
        templater_logger.debug("    Templated String: %r", templated_str)
        # TRICKY: Note that the templated_str parameter is not used. JinjaTracer
        # uses make_template() to build and render the template itself.
        analyzer = JinjaAnalyzer(raw_str, self._get_jinja_env())
        tracer = analyzer.analyze(make_template)
        trace = tracer.trace(append_to_templated=kwargs.pop("append_to_templated", ""))
        return trace.raw_sliced, trace.sliced_file, trace.templated_str


class DummyUndefined(jinja2.Undefined):
    """Acts as a dummy value to try and avoid template failures.

    Inherits from jinja2.Undefined so Jinja's default() filter will
    treat it as a missing value, even though it has a non-empty value
    in normal contexts.
    """

    # Tell Jinja this object is safe to call and does not alter data.
    # https://jinja.palletsprojects.com/en/2.9.x/sandbox/#jinja2.sandbox.SandboxedEnvironment.is_safe_callable
    unsafe_callable = False
    # https://jinja.palletsprojects.com/en/3.0.x/sandbox/#jinja2.sandbox.SandboxedEnvironment.is_safe_callable
    alters_data = False

    def __init__(self, name):
        super().__init__()
        self.name = name

    def __str__(self):
        return self.name.replace(".", "_")

    @classmethod
    def create(cls, name):
        """Factory method.

        When ignoring=templating is configured, use 'name' as the value for
        undefined variables. We deliberately avoid recording and reporting
        undefined variables as errors. Using 'name' as the value won't always
        work, but using 'name', combined with implementing the magic methods
        (such as __eq__, see above), works well in most cases.
        """
        templater_logger.debug(
            "Providing dummy value for undefined Jinja variable: %s", name
        )
        result = DummyUndefined(name)
        return result

    def __getattr__(self, item):
        return self.create(f"{self.name}.{item}")

    # Implement the most common magic methods. This helps avoid
    # templating errors for undefined variables.
    # https://www.tutorialsteacher.com/python/magic-methods-in-python
    def _self_impl(self, *args, **kwargs):
        return self

    def _bool_impl(self, *args, **kwargs):
        return True

    __add__ = _self_impl
    __sub__ = _self_impl
    __mul__ = _self_impl
    __floordiv__ = _self_impl
    __truediv__ = _self_impl
    __mod__ = _self_impl
    __pow__ = _self_impl
    __pos__ = _self_impl
    __neg__ = _self_impl
    __lshift__ = _self_impl
    __rshift__ = _self_impl
    __getitem__ = _self_impl
    __invert__ = _self_impl
    __call__ = _self_impl
    __and__ = _bool_impl
    __or__ = _bool_impl
    __xor__ = _bool_impl
    __bool__ = _bool_impl
    __lt__ = _bool_impl
    __le__ = _bool_impl
    __eq__ = _bool_impl
    __ne__ = _bool_impl
    __ge__ = _bool_impl
    __gt__ = _bool_impl

    def __hash__(self):  # pragma: no cov
        # This is called by the "in" operator, among other things.
        return 0

    def __iter__(self):
        return [self].__iter__()
