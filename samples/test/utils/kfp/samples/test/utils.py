# Copyright 2021 The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import json
import logging
import os
from pprint import pprint
import random
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional
import unittest

from google.protobuf.json_format import MessageToDict
import kfp
import kfp.compiler
import kfp_server_api
from ml_metadata import metadata_store
from ml_metadata.metadata_store.metadata_store import ListOptions
from ml_metadata.proto import Event
from ml_metadata.proto import Execution
from ml_metadata.proto import metadata_store_pb2
from nbconvert import PythonExporter
import nbformat

MINUTE = 60

logger = logging.getLogger('kfp.test.sampleutils')
logger.setLevel('INFO')


# Add **kwargs, so that when new arguments are added, this doesn't fail for
# unknown arguments.
def _default_verify_func(
        run_id: int, run: kfp_server_api.ApiRun,
        mlmd_connection_config: metadata_store_pb2.MetadataStoreClientConfig,
        **kwargs):
    assert run.status == 'Succeeded'


def NEEDS_A_FIX(run_id, run, **kwargs):
    """confirms a sample test case is failing and it needs to be fixed."""
    assert run.status == 'Failed'


Verifier = Callable[[
    int, kfp_server_api.ApiRun, kfp_server_api.ApiRunDetail, metadata_store_pb2
    .MetadataStoreClientConfig
], None]


@dataclass
class TestCase:
    """Test case for running a KFP sample. One of pipeline_func or
    pipeline_file is required.

    :param run_pipeline: when False, it means the test case just runs the python file.
    :param pipeline_file_compile_path: when specified, the pipeline file can compile
           to a pipeline package by itself, and the package is expected to be stored
           in pipeline_file_compile_path.
    """
    pipeline_func: Optional[Callable] = None
    pipeline_file: Optional[str] = None
    pipeline_file_compile_path: Optional[str] = None
    enable_caching: bool = False
    arguments: Optional[dict[str, str]] = None
    verify_func: Verifier = _default_verify_func
    run_pipeline: bool = True
    timeout_mins: float = 20.0


def run_pipeline_func(test_cases: list[TestCase]):
    """Run a pipeline function and wait for its result.

    :param pipeline_func: pipeline function to run
    :type pipeline_func: function
    """

    if not test_cases:
        raise ValueError('No test cases!')

    def test_wrapper(
        run_pipeline: Callable[[Callable, str, str, bool, dict, bool],
                               kfp_server_api.ApiRunDetail],
        mlmd_connection_config: metadata_store_pb2.MetadataStoreClientConfig,
    ):
        for case in test_cases:
            pipeline_name = None
            if (not case.pipeline_file) and (not case.pipeline_func):
                raise ValueError(
                    'TestCase must have exactly one of pipeline_file or pipeline_func specified, got none.'
                )
            if case.pipeline_file and case.pipeline_func:
                raise ValueError(
                    'TestCase must have exactly one of pipeline_file or pipeline_func specified, got both.'
                )
            if case.pipeline_func:
                # TODO: remove accessing of protected member
                pipeline_name = getattr(
                    case.pipeline_func, 'name',
                    getattr(case.pipeline_func, '_component_human_name',
                            'pipeline'))
            else:
                pipeline_name = os.path.basename(case.pipeline_file)
            if not case.run_pipeline:
                if not case.pipeline_file:
                    raise ValueError(
                        'TestCase.run_pipeline = False can only be specified when used together with pipeline_file.'
                    )

            run_detail = run_pipeline(
                pipeline_func=case.pipeline_func,
                pipeline_file=case.pipeline_file,
                pipeline_file_compile_path=case.pipeline_file_compile_path,
                enable_caching=case.enable_caching,
                arguments=case.arguments or {},
                dry_run=not case.run_pipeline,
                timeout=case.timeout_mins * MINUTE)
            if not case.run_pipeline:
                # There is no run_detail.
                print(f'Test case {pipeline_name} passed!')
                continue
            pipeline_runtime: kfp_server_api.ApiPipelineRuntime = run_detail.pipeline_runtime
            argo_workflow = json.loads(pipeline_runtime.workflow_manifest)
            argo_workflow_name = argo_workflow.get('metadata').get('name')
            print(f'argo workflow name: {argo_workflow_name}')
            t = unittest.TestCase()
            t.maxDiff = None  # we always want to see full diff
            tasks = {}
            client = None
            case.verify_func(
                run=run_detail.run,
                run_detail=run_detail,
                run_id=run_detail.run.id,
                mlmd_connection_config=mlmd_connection_config,
                argo_workflow_name=argo_workflow_name,
                t=t,
                tasks=tasks,
                client=client,
            )
        print('OK: all test cases passed!')

    _run_test(test_wrapper)


def debug_verify(run_id: str, verify_func: Verifier):
    """Debug a verify function quickly using MLMD data from an existing KFP run
    ID."""
    t = unittest.TestCase()
    t.maxDiff = None  # we always want to see full diff
    client = KfpMlmdClient()
    tasks = client.get_tasks(run_id=run_id)
    pprint(tasks)

    verify_func(
        run=kfp_server_api.ApiRun(id=run_id, status='Succeeded'),
        run_id=run_id,
        t=t,
        tasks=tasks,
        client=client,
    )


def _retry_with_backoff(fn: Callable, retries=3, backoff_in_seconds=1):
    i = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if i >= retries:
                print(f"Failed after {retries} retries:")
                raise
            else:
                print(e)
                sleep = (backoff_in_seconds * 2**i + random.uniform(0, 1))
                print("  Retry after ", str(sleep) + "s")
                time.sleep(sleep)
                i += 1


def _run_test(callback):

    def main(
        pipeline_root: Optional[str] = None,  # example
        experiment: str = 'v2_sample_test_samples',
        metadata_service_host: Optional[str] = None,
        metadata_service_port: int = 8080,
    ):
        """Test file CLI entrypoint used by Fire. To configure KFP endpoint,
        configure env vars following:
        https://www.kubeflow.org/docs/components/pipelines/user-guides/core-functions/connect-api/#configure-sdk-client-by-environment-variables. KFP UI endpoint can
        be configured by KF_PIPELINES_UI_ENDPOINT env var.

        :param pipeline_root: pipeline root that holds intermediate
        artifacts, example gs://your-bucket/path/to/workdir.
        :type pipeline_root: str, optional
        :param experiment: experiment the run is added to, defaults to 'v2_sample_test_samples'
        :type experiment: str, optional
        :param metadata_service_host: host for metadata grpc service, defaults to METADATA_GRPC_SERVICE_HOST or 'metadata-grpc-service'
        :type metadata_service_host: str, optional
        :param metadata_service_port: port for metadata grpc service, defaults to 8080
        :type metadata_service_port: int, optional
        """

        if pipeline_root is None:
            pipeline_root = os.getenv('KFP_PIPELINE_ROOT')
            if not pipeline_root:
                pipeline_root = os.getenv('KFP_OUTPUT_DIRECTORY')
                if pipeline_root:
                    logger.warning(
                        f'KFP_OUTPUT_DIRECTORY env var is left for backward compatibility, please use KFP_PIPELINE_ROOT instead.'
                    )
        logger.info(f'KFP_PIPELINE_ROOT={pipeline_root}')
        if metadata_service_host is None:
            metadata_service_host = os.getenv('METADATA_GRPC_SERVICE_HOST',
                                              'metadata-grpc-service')
        logger.info(f'METADATA_GRPC_SERVICE_HOST={metadata_service_host}')
        client = kfp.Client()
        # TODO(Bobgy): avoid using private fields when getting loaded config
        kfp_endpoint = client._existing_config.host
        kfp_ui_endpoint = client._uihost
        logger.info(f'KF_PIPELINES_ENDPOINT={kfp_endpoint}')
        if kfp_ui_endpoint != kfp_endpoint:
            logger.info(f'KF_PIPELINES_UI_ENDPOINT={kfp_ui_endpoint}')

        def run_pipeline(
            pipeline_func: Optional[Callable],
            pipeline_file: Optional[str],
            pipeline_file_compile_path: Optional[str],
            enable_caching: bool = False,
            arguments: Optional[dict] = None,
            dry_run: bool = False,  # just compile the pipeline without running it
            timeout: float = 20 * MINUTE,
        ) -> kfp_server_api.ApiRunDetail:
            arguments = arguments or {}

            def _create_run():
                return run_v2_pipeline(
                    client=client,
                    fn=pipeline_func,
                    file=pipeline_file,
                    pipeline_root=pipeline_root,
                    enable_caching=enable_caching,
                    arguments={
                        **arguments,
                    },
                )

            run_result = _retry_with_backoff(fn=_create_run)
            if dry_run:
                # There is no run_result when dry_run.
                return
            print("Run details page URL:")
            print(f"{kfp_ui_endpoint}/#/runs/details/{run_result.run_id}")
            run_detail = run_result.wait_for_run_completion(timeout)
            # Hide detailed information for pretty printing
            workflow_spec = run_detail.run.pipeline_spec.workflow_manifest
            workflow_manifest = run_detail.pipeline_runtime.workflow_manifest
            run_detail.run.pipeline_spec.workflow_manifest = None
            run_detail.pipeline_runtime.workflow_manifest = None
            pprint(run_detail)
            # Restore workflow manifest, because test cases may use it
            run_detail.run.pipeline_spec.workflow_manifest = workflow_spec
            run_detail.pipeline_runtime.workflow_manifest = workflow_manifest
            return run_detail

        # When running locally, port forward MLMD grpc service to localhost:8080 by:
        #
        # 1. NAMESPACE=kubeflow kubectl port-forward svc/metadata-grpc-service 8080:8080 -n $NAMESPACE
        # 2. Configure env var METADATA_GRPC_SERVICE_HOST=localhost.
        mlmd_connection_config = metadata_store_pb2.MetadataStoreClientConfig(
            host=metadata_service_host,
            port=metadata_service_port,
        )
        callback(
            run_pipeline=run_pipeline,
            mlmd_connection_config=mlmd_connection_config)

    import fire
    fire.Fire(main)


def run_v2_pipeline(
    client: kfp.Client,
    fn: Optional[Callable],
    file: Optional[str],
    pipeline_root: Optional[str],
    enable_caching: bool,
    arguments: dict[str, str],
):
    pipeline_spec_file = tempfile.mktemp(
        suffix='.yaml', prefix="original_pipeline_spec")
    if fn:
        kfp.compiler.Compiler().compile(
            pipeline_func=fn, package_path=pipeline_spec_file)
    else:
        pyfile = file
        if file.endswith(".ipynb"):
            pyfile = tempfile.mktemp(suffix='.py', prefix="pipeline_py_code")
            _nb_sample_to_py(file, pyfile)
        from kfp.cli.compile import dsl_compile
        dsl_compile(py=pyfile, output=pipeline_spec_file)

    return client.create_run_from_pipeline_package(
        pipeline_file=pipeline_spec_file,
        arguments={},
        pipeline_root=pipeline_root,
        enable_caching=enable_caching)


def _simplify_proto_struct(data: dict) -> dict:
    res = {}
    for key, value in data.items():
        if value.get('stringValue') is not None:
            res[key] = value['stringValue']
        elif value.get('doubleValue') is not None:
            res[key] = value['doubleValue']
        elif value.get('structValue') is not None:
            res[key] = value['structValue']
        else:
            res[key] = value
    return res


@dataclass
class KfpArtifact:
    name: str
    uri: str
    type: str
    metadata: dict

    @classmethod
    def new(
        cls,
        mlmd_artifact: metadata_store_pb2.Artifact,
        mlmd_artifact_type: metadata_store_pb2.ArtifactType,
        mlmd_event: metadata_store_pb2.Event,
    ):
        # event path is conceptually input/output name in a task
        # ref: https://github.com/google/ml-metadata/blob/78ea886c18979d79f3c224092245873474bfafa2/ml_metadata/proto/metadata_store.proto#L169-L180
        artifact_name = mlmd_event.path.steps[0].key
        # The original field is custom_properties, but MessageToDict converts it
        # to customProperties.
        metadata = _simplify_proto_struct(
            MessageToDict(mlmd_artifact).get('customProperties', {}))
        return cls(
            name=artifact_name,
            type=mlmd_artifact_type.name,
            uri=mlmd_artifact.uri,
            metadata=metadata)


@dataclass
class TaskInputs:
    parameters: dict
    artifacts: list[KfpArtifact]


@dataclass
class TaskOutputs:
    parameters: dict
    artifacts: list[KfpArtifact]


@dataclass
class KfpTask:
    """A KFP runtime task."""
    name: str
    type: str
    state: int
    inputs: TaskInputs
    outputs: TaskOutputs
    children: Optional[dict[str, KfpTask]] = None

    def get_dict(self):
        # Keep inputs and outputs keys, but ignore other zero values.
        ignore_zero_values_except_io = lambda x: {
            k: v for (k, v) in x if k in ["inputs", "outputs"] or v
        }
        d = asdict(self, dict_factory=ignore_zero_values_except_io)
        # remove uri, because they are not deterministic
        for artifact in d.get('inputs', {}).get('artifacts', []):
            artifact.pop('uri')
        for artifact in d.get('outputs', {}).get('artifacts', []):
            artifact.pop('uri')
        # children should be accessed separately
        if d.get('children') is not None:
            d.pop('children')
        return d

    def __repr__(self, depth=1):
        return_string = [str(self.get_dict())]
        if self.children:
            for child in self.children.values():
                return_string.extend(
                    ["\n", "--" * depth,
                     child.__repr__(depth + 1)])
        return "".join(return_string)

    @classmethod
    def new(
        cls,
        execution: metadata_store_pb2.Execution,
        execution_types_by_id: dict[int, metadata_store_pb2.ExecutionType],
        events_by_execution_id: dict[int, list[metadata_store_pb2.Event]],
        artifacts_by_id: dict[int, metadata_store_pb2.Artifact],
        artifact_types_by_id: dict[int, metadata_store_pb2.ArtifactType],
        children: Optional[dict[str, KfpTask]],
    ):
        name = execution.custom_properties.get('task_name').string_value
        iteration_index = execution.custom_properties.get('iteration_index')
        if iteration_index:
            name += f'-#{iteration_index.int_value}'
        execution_type = execution_types_by_id[execution.type_id]
        params = _parse_parameters(execution)
        events = events_by_execution_id.get(execution.id, [])
        input_artifacts = []
        output_artifacts = []
        if events:
            input_artifacts_info = [(e.artifact_id, e)
                                    for e in events
                                    if e.type == metadata_store_pb2.Event.INPUT]
            output_artifacts_info = [
                (e.artifact_id, e)
                for e in events
                if e.type == metadata_store_pb2.Event.OUTPUT
            ]

            def kfp_artifact(aid: int,
                             e: metadata_store_pb2.Event) -> KfpArtifact:
                mlmd_artifact = artifacts_by_id[aid]
                mlmd_type = artifact_types_by_id[mlmd_artifact.type_id]
                return KfpArtifact.new(
                    mlmd_artifact=mlmd_artifact,
                    mlmd_artifact_type=mlmd_type,
                    mlmd_event=e,
                )

            input_artifacts = [
                kfp_artifact(aid, e) for (aid, e) in input_artifacts_info
            ]
            input_artifacts.sort(key=lambda a: a.name)
            output_artifacts = [
                kfp_artifact(aid, e) for (aid, e) in output_artifacts_info
            ]
            output_artifacts.sort(key=lambda a: a.name)

        return cls(
            name=name,
            type=execution_type.name,
            state=execution.last_known_state,
            inputs=TaskInputs(
                parameters=params['inputs'], artifacts=input_artifacts),
            outputs=TaskOutputs(
                parameters=params['outputs'], artifacts=output_artifacts),
            children=children or None,
        )


class KfpMlmdClient:

    def __init__(
        self,
        mlmd_connection_config: Optional[
            metadata_store_pb2.MetadataStoreClientConfig] = None,
    ):
        if mlmd_connection_config is None:
            # default to value suitable for local testing
            mlmd_connection_config = metadata_store_pb2.MetadataStoreClientConfig(
                host='localhost',
                port=8080,
            )
        self.mlmd_store = metadata_store.MetadataStore(mlmd_connection_config)
        self.dag_type = self.mlmd_store.get_execution_type(
            type_name='system.DAGExecution')

    def get_tasks(self, run_id: str):
        run_context = self.mlmd_store.get_context_by_type_and_name(
            type_name='system.PipelineRun',
            context_name=run_id,
        )
        if not run_context:
            raise Exception(
                f'Cannot find system.PipelineRun context "{run_id}"')
        logger.info(f'run_context: name={run_context.name} id={run_context.id}')

        root = self.mlmd_store.get_execution_by_type_and_name(
            type_name='system.DAGExecution',
            execution_name=f'run/{run_id}',
        )
        if not root:
            raise Exception(
                f'Cannot find system.DAGExecution execution "run/{run_id}"')
        logger.info(f'root_dag: name={root.name} id={root.id}')
        return self._get_tasks(root.id, run_context.id)

    def _get_tasks(self, dag_id: int,
                   run_context_id: int) -> dict[str, KfpTask]:
        # Note, we only need to query by parent_dag_id. However, there is no index
        # on parent_dag_id. To speed up the query, we also limit the query to the
        # run context (contexts have index).
        filter_query = f'contexts_run.id = {run_context_id} AND custom_properties.parent_dag_id.int_value = {dag_id}'
        executions = self.mlmd_store.get_executions(
            list_options=ListOptions(filter_query=filter_query))
        execution_types = self.mlmd_store.get_execution_types_by_id(
            list(set([e.type_id for e in executions])))
        execution_types_by_id = {et.id: et for et in execution_types}
        events = self.mlmd_store.get_events_by_execution_ids(
            [e.id for e in executions])
        events_by_execution_id = {}
        for e in events:
            events_by_execution_id[e.execution_id] = (
                events_by_execution_id.get(e.execution_id) or []) + [e]
        artifacts = self.mlmd_store.get_artifacts_by_id(
            artifact_ids=[e.artifact_id for e in events])
        artifacts_by_id = {a.id: a for a in artifacts}
        artifact_types = self.mlmd_store.get_artifact_types_by_id(
            list(set([a.type_id for a in artifacts])))
        artifact_types_by_id = {at.id: at for at in artifact_types}
        _validate_executions_have_task_names(executions)

        def get_children(e: Execution) -> Optional[dict[str, KfpTask]]:
            if e.type_id == self.dag_type.id:
                children = self._get_tasks(e.id, run_context_id)
                return children
            return None

        tasks = [
            KfpTask.new(
                execution=e,
                execution_types_by_id=execution_types_by_id,
                events_by_execution_id=events_by_execution_id,
                artifacts_by_id=artifacts_by_id,
                artifact_types_by_id=artifact_types_by_id,
                children=get_children(e),
            ) for e in executions
        ]
        tasks_by_name = {t.name: t for t in tasks}
        return tasks_by_name


def _validate_executions_have_task_names(execution_list):
    executions_without_task_name = [
        e for e in execution_list
        if not e.custom_properties.get('task_name').string_value
    ]
    if executions_without_task_name:
        raise Exception(
            f'some executions are missing task_name custom property. executions:\n{executions_without_task_name}'
        )


def _parse_parameters(execution: metadata_store_pb2.Execution) -> dict:
    custom_properties = execution.custom_properties
    parameters = {'inputs': {}, 'outputs': {}}
    for item in custom_properties.items():
        (name, value) = item
        raw_value = None
        if value.HasField('string_value'):
            raw_value = value.string_value
        if value.HasField('int_value'):
            raw_value = value.int_value
        if value.HasField('double_value'):
            raw_value = value.double_value
        if name.startswith('input:'):
            parameters['inputs'][name[len('input:'):]] = raw_value
        if name.startswith('output:'):
            parameters['outputs'][name[len('output:'):]] = raw_value
        if name == "inputs" and value.HasField('struct_value'):
            for k, v in _simplify_proto_struct(
                    MessageToDict(value))["structValue"].items():
                parameters['inputs'][k] = v
        if name == "outputs" and value.HasField('struct_value'):
            for k, v in _simplify_proto_struct(
                    MessageToDict(value))["structValue"].items():
                parameters['outputs'][k] = v
    return parameters


def _nb_sample_to_py(notebook_path: str, output_path: str):
    """nb_sample_to_py converts notebook kfp sample to a python file.

    Cells with tag "skip-in-test" will be omitted.
    """
    with open(notebook_path, 'r') as f:
        nb = nbformat.read(f, as_version=4)
        # Cells with skip-in-test tag will be omitted.
        # Example code that needs the tag:
        # kfp.Client().create_run_from_pipeline_func()
        # so that we won't submit pipelines when compiling them.
        nb.cells = [
            cell for cell in nb.cells
            if 'skip-in-test' not in cell.get('metadata', {}).get('tags', [])
        ]
        py_exporter = PythonExporter()
        (py_code, res) = py_exporter.from_notebook_node(nb)
        with open(output_path, 'w') as out:
            out.write(py_code)


def relative_path(file_path: str, relative_path: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.realpath(file_path)), relative_path)
