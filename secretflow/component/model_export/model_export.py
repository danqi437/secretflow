# Copyright 2024 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import copy
import inspect
from collections import defaultdict

import pyarrow as pa
from google.protobuf import json_format
from secretflow_spec.v1.data_pb2 import DistData
from secretflow_spec.v1.evaluation_pb2 import NodeEvalParam
from secretflow_spec.v1.report_pb2 import Report

from secretflow.component.core import (
    Component,
    Context,
    Definition,
    DistDataType,
    Field,
    IServingExporter,
    Output,
    Registry,
    ServingBuilder,
    ServingNode,
    ServingPhase,
    VTable,
    VTableSchema,
    register,
    uuid4,
)
from secretflow.compute.tracer import Table
from secretflow.device import PYU, PYUObject, reveal
from secretflow.utils.errors import InvalidArgumentError, NotSupportedError


@register(domain="model", version="1.0.0")
class ModelExport(Component):
    '''
    The model_export component supports converting and
    packaging the rule files generated by preprocessing and
    postprocessing components, as well as the model files generated
    by model operators, into a Secretflow-Serving model package. The
    list of components to be exported must contain exactly one model
    train or model predict component, and may include zero or
    multiple preprocessing and postprocessing components.
    '''

    model_name: str = Field.attr(desc="model's name")
    model_desc: str = Field.attr(desc="Describe what the model does", default="")
    input_datasets: list[str] = Field.attr(
        desc=(
            "The input data IDs for all components to be exported. "
            "Their order must remain consistent with the sequence in which the components were executed."
        )
    )
    output_datasets: list[str] = Field.attr(
        desc=(
            "The output data IDs for all components to be exported. "
            "Their order must remain consistent with the sequence in which the components were executed."
        )
    )
    component_eval_params: list[str] = Field.attr(
        desc=(
            "The eval parameters (in JSON format) for all components to be exported. "
            "Their order must remain consistent with the sequence in which the components were executed."
        )
    )
    he_mode: bool = Field.attr(
        desc="If enabled, it will export a homomorphic encryption model. Currently, only SGD and GLM models for two-party scenarios are supported.",
        default=False,
    )
    output_package: Output = Field.output(
        desc="output tar package uri",
        types=[DistDataType.SERVING_MODEL],
    )
    report: Output = Field.output(
        desc="report dumped model's input schemas",
        types=[DistDataType.REPORT],
    )

    def evaluate(self, ctx: Context):
        input_datasets = [json_format.Parse(i, DistData()) for i in self.input_datasets]
        output_datasets = [
            json_format.Parse(o, DistData()) for o in self.output_datasets
        ]
        component_eval_params = [
            json_format.Parse(base64.b64decode(o).decode("utf-8"), NodeEvalParam())
            for o in self.component_eval_params
        ]
        if not input_datasets or not output_datasets or not component_eval_params:
            raise InvalidArgumentError(
                "input_datasets, output_datasets and component_eval_Params must be set"
            )
        system_info = input_datasets[0].system_info

        init_tbl = self.get_init_table(input_datasets, component_eval_params)
        pyus = [PYU(party) for party in init_tbl.parties.keys()]

        builder = ServingBuilder(pyus)
        for param in component_eval_params:
            comp_def = Registry.get_definition_by_id(param.comp_id)
            if comp_def is None:
                raise ValueError(f"unknown component: {param.comp_id}")
            minor = Definition.parse_minor(comp_def.version)
            input_defs = comp_def.get_input_defs(minor)
            output_defs = comp_def.get_output_defs(minor)
            inputs = input_datasets[: len(input_defs)]
            outputs = output_datasets[: len(output_defs)]
            input_datasets = input_datasets[len(input_defs) :]
            output_datasets = output_datasets[len(output_defs) :]

            comp_kwargs = comp_def.parse_param(param, inputs, outputs)
            comp: Component = comp_def.make_component(comp_kwargs)
            if not isinstance(comp, IServingExporter):
                raise NotSupportedError(
                    "The component does not implement the IServingExporter interface",
                    detail={"component": comp.__class__.__name__},
                )
            export_kwargs = self.get_export_kwargs(comp)
            comp.export(ctx, builder, **export_kwargs)

        used_columns = self.rebuild_preprocessing_nodes(builder, init_tbl.schemas)
        self.dump_tar_files(ctx, builder, system_info)
        self.dump_report(used_columns, system_info)

    @staticmethod
    def get_init_table(
        input_datasets, component_eval_params: list[NodeEvalParam]
    ) -> VTable:
        first_param = component_eval_params[0]
        comp_def = Registry.get_definition_by_id(first_param.comp_id)
        minor = Definition.parse_minor(comp_def.version)
        dist_datas = input_datasets[: len(comp_def.get_input_defs(minor))]
        v_tables = [d for d in dist_datas if d.type == DistDataType.VERTICAL_TABLE]
        if len(v_tables) != 1:
            raise InvalidArgumentError("only support one vertical_table input for now")
        return VTable.from_distdata(v_tables[0])

    def get_export_kwargs(self, comp: Component) -> dict:
        sig = inspect.signature(comp.export)
        kwargs = {}
        for name, param in sig.parameters.items():
            param_type = param.annotation
            if param_type is not param.empty and param_type in [
                Context,
                ServingBuilder,
            ]:
                continue

            if name in self.__dict__:
                kwargs[name] = self.__dict__[name]

        return kwargs

    @staticmethod
    def rebuild_preprocessing_nodes(
        builder: ServingBuilder, init_schemas: dict[str, VTableSchema]
    ) -> list[str]:
        assert len(builder.nodes) > 0, f"empty nodes"
        if builder.nodes[0].phase not in [
            ServingPhase.PREPROCESSING,
            ServingPhase.TRAIN_PREDICT,
        ]:
            return []

        nodes: list[ServingNode] = []
        for node in builder.nodes:
            assert node.phase in [
                ServingPhase.PREPROCESSING,
                ServingPhase.TRAIN_PREDICT,
            ], f"invalid {node.phase}"
            nodes.append(node)
            if node.phase == ServingPhase.TRAIN_PREDICT:
                break

        if len(nodes) == 0:
            return []

        init_columns = {PYU(party): set(s.names) for party, s in init_schemas.items()}

        last_node = nodes.pop()
        trace_schemas = copy.deepcopy(last_node.input_schemas)

        for node in reversed(nodes):
            for pyu in builder.pyus:
                trace_schema = trace_schemas.get(pyu, pa.schema([]))
                if pyu not in node.input_schemas:
                    # build empty node
                    assert pyu not in node.output_schemas
                    assert pyu not in node.kwargs
                    table = Table.from_schema(trace_schema)
                    dag, in_schema, out_schema = table.dump_serving_pb(node.name)
                else:
                    trace_schema, in_schema, out_schema = ModelExport.rebuild_schema(
                        trace_schema, node.input_schemas[pyu], node.output_schemas[pyu]
                    )
                    trace_schemas[pyu] = trace_schema
                    dag = node.kwargs[pyu]["trace_content"]
                kwargs = ServingNode.build_arrow_processing_kwargs(
                    in_schema, out_schema, dag
                )
                node.add(pyu, in_schema, out_schema, kwargs)

        invalid_columns = defaultdict(list)
        used_columns = []
        for pyu, schema in trace_schemas.items():
            party_init_columns = init_columns[pyu]
            for f in schema:
                if f.name not in party_init_columns:
                    invalid_columns[pyu.party].append(f.name)
                else:
                    used_columns.append(f.name)

        if len(invalid_columns) > 0:
            raise ValueError(f"invalid columns {invalid_columns}")

        return used_columns

    @staticmethod
    def rebuild_schema(
        trace_schema: pa.Schema, in_schema: pa.Schema, out_schema: pa.Schema
    ) -> tuple[pa.Schema, pa.Schema, pa.Schema]:
        if isinstance(in_schema, PYUObject):
            in_schema = reveal(in_schema)
        if isinstance(out_schema, PYUObject):
            out_schema = reveal(out_schema)
        new_trace_fields, new_in_fields, new_out_fields = [], [], []
        for f in trace_schema:
            name = f.name
            is_input = name in in_schema.names
            is_output = name in out_schema.names
            is_added = not is_input and is_output
            if is_added:
                continue

            new_trace_fields.append(f)
            if not is_input:
                new_in_fields.append(f)
            if not is_output:
                new_out_fields.append(f)

        for f in in_schema:
            if f.name not in trace_schema.names:
                new_trace_fields.append(f)
        trace_schema = pa.schema(new_trace_fields)

        for f in new_in_fields:
            in_schema = in_schema.append(f)
        for f in new_out_fields:
            out_schema = out_schema.append(f)

        return trace_schema, in_schema, out_schema

    def dump_tar_files(
        self, ctx: Context, builder: ServingBuilder, system_info
    ) -> None:
        pyus = builder.pyus
        model_name = f"{self.model_name}_{uuid4(pyus[0])}"
        model_desc = self.model_desc
        uri = self.output_package.uri

        builder.dump_tar_files(ctx.storage, model_name, model_desc, uri)

        model_dd = DistData(
            name=model_name,
            type=str(DistDataType.SERVING_MODEL),
            system_info=system_info,
            data_refs=[
                DistData.DataRef(uri=uri, party=p.party, format="tar.gz") for p in pyus
            ],
        )
        self.output_package.data = model_dd

    def dump_report(self, used_columns: list[str], system_info):
        report_meta = Report(
            name="used schemas",
            desc=",".join(used_columns),
        )

        report_dd = DistData(
            name=self.report.uri,
            type=str(DistDataType.REPORT),
            system_info=system_info,
        )
        report_dd.meta.Pack(report_meta)
        self.report.data = report_dd
