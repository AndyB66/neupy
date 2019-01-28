import re
import sys
import copy
import types
import inspect
import tempfile
from itertools import chain
from functools import wraps
from abc import abstractmethod
from collections import OrderedDict, defaultdict

import six
import graphviz
import numpy as np
import tensorflow as tf

from neupy.core.config import ConfigurableABC, DumpableObject
from neupy.exceptions import LayerConnectionError
from neupy.core.properties import Property, TypedListProperty
from neupy.utils import as_tuple, tf_utils


__all__ = (
    'BaseGraph', 'LayerGraph',
    'BaseLayer', 'Identity', 'Input',
    'join', 'parallel', 'merge',
)


def make_one_if_possible(shape):
    """
    Format layer's input or output shape.

    Parameters
    ----------
    shape : int or tuple

    Returns
    -------
    int or tuple
    """
    if isinstance(shape, (tuple, list)) and len(shape) == 1:
        return shape[0]
    return shape


def filter_graph(dictionary, include_keys):
    """
    Create new list that contains only values
    specified in the ``include_keys`` attribute.

    Parameters
    ----------
    dictionary : dict
        Original dictionary

    include_keys : list or tuple
        Keys that will copied from original dictionary
        into a new one.

    Returns
    -------
    dict
    """
    filtered_dict = OrderedDict()

    for key, value in dictionary.items():
        if key in include_keys:
            filtered_dict[key] = [v for v in value if v in include_keys]

    return filtered_dict


def is_cyclic(graph):
    """
    Check if graph has cycles.

    Parameters
    ----------
    graph : dict
        must be represented as a dictionary mapping vertices to
        iterables of neighbouring vertices.

    Returns
    -------
    bool
        Return ``True`` if the directed graph has a cycle.

    Examples
    --------
    >>> is_cyclic({1: [2], 2: [3], 3: [1]})
    True
    >>> is_cyclic({1: [2], 2: [3], 3: [4]})
    False
    """
    path = set()
    visited = set()

    def visit(vertex):
        if vertex in visited:
            return False

        visited.add(vertex)
        path.add(vertex)

        for neighbour in graph.get(vertex, ()):
            if neighbour in path or visit(neighbour):
                return True

        path.remove(vertex)
        return False

    return any(visit(vertex) for vertex in graph)


def find_outputs_in_graph(graph):
    outputs = []

    for from_node, to_nodes in graph.items():
        if not to_nodes:
            outputs.append(from_node)

    return outputs


def topological_sort(graph):
    """
    Repeatedly go through all of the nodes in the graph, moving each of
    the nodes that has all its edges resolved, onto a sequence that
    forms our sorted graph. A node has all of its edges resolved and
    can be moved once all the nodes its edges point to, have been moved
    from the unsorted graph onto the sorted one.

    Parameters
    ----------
    graph : dict
        Dictionary that has graph structure.

    Raises
    ------
    RuntimeError
        If graph has cycles.

    Returns
    -------
    list
        List of nodes sorted in topological order.
    """
    if not graph:
        return []

    if is_cyclic(graph):
        raise RuntimeError(
            "Cannot apply topological sort to the graphs with cycles")

    sorted_nodes = []
    graph_unsorted = graph.copy()

    while graph_unsorted:
        for node, edges in list(graph_unsorted.items()):
            if all(edge not in graph_unsorted for edge in edges):
                del graph_unsorted[node]
                sorted_nodes.append(node)

    return sorted_nodes


def lazy_property(function):
    attr = '_lazy__' + function.__name__

    @property
    @wraps(function)
    def wrapper(self):
        if not hasattr(self, attr):
            setattr(self, attr, function(self))
        return getattr(self, attr)

    return wrapper


class BaseGraph(ConfigurableABC, DumpableObject):
    events = []

    def __init__(self, forward_graph=None):
        self.forward_graph = OrderedDict(forward_graph or [])

    @lazy_property
    def backward_graph(self):
        # First we copy all the nodes in order to
        # make sure that order stays the same
        backward = OrderedDict([(node, []) for node in self.forward_graph])

        for to_node, from_nodes in self.forward_graph.items():
            for from_node in from_nodes:
                backward[from_node].append(to_node)

        return backward

    @lazy_property
    def input_layers(self):
        return find_outputs_in_graph(self.backward_graph)

    @lazy_property
    def output_layers(self):
        return find_outputs_in_graph(self.forward_graph)

    @lazy_property
    def inputs(self):
        placeholders = []

        for layer in self.input_layers:
            placeholder = tf.placeholder(
                tf.float32,
                shape=tf_utils.shape_to_tuple(layer.input_shape),
                name="placeholder/input/{}".format(layer.name),
            )
            placeholders.append(placeholder)

        return make_one_if_possible(placeholders)

    @lazy_property
    def targets(self):
        placeholders = []

        for layer in self.output_layers:
            placeholder = tf.placeholder(
                tf.float32,
                shape=tf_utils.shape_to_tuple(layer.output_shape),
                name="placeholder/target/{}".format(layer.name),
            )
            placeholders.append(placeholder)

        return make_one_if_possible(placeholders)

    @lazy_property
    def outputs(self):
        networks_output = self.output(*as_tuple(self.inputs))
        tf_utils.initialize_uninitialized_variables()
        return networks_output

    @lazy_property
    def training_outputs(self):
        networks_output = self.output(*as_tuple(self.inputs), training=True)
        tf_utils.initialize_uninitialized_variables()
        return networks_output

    def __gt__(self, other):
        left, right = self, other
        self.events.append(('__gt__', join(left, right)))

        graph = LayerGraph()
        previous_operator = None

        for operator, value in reversed(self.events):
            if operator == previous_operator:
                break

            if operator == '__gt__':
                # It's important to put `value` before graph, because
                # we merge in reverse order and we need to make sure
                # that every new value has higher priority.
                graph = merge(value, graph)

            previous_operator = operator

        return graph

    def __bool__(self):
        self.events.append(('__bool__', self))
        return True

    def __nonzero__(self):
        return self.__bool__()  # Hack for python 2

    def __rshift__(self, other):
        return join(self, other)

    def __irshift__(self, other):
        return self.__rshift__(other)

    def __or__(self, other):
        return parallel(self, other)

    def __ior__(self, other):
        return self.__or__(other)

    @abstractmethod
    def output(self, inputs):
        raise NotImplementedError()

    @property
    @abstractmethod
    def output_shape(self):
        raise NotImplementedError()

    @abstractmethod
    def get_output_shape(self, input_shape):
        raise NotImplementedError()


class LayerGraph(BaseGraph):
    def __init__(self, forward_graph=None):
        super(LayerGraph, self).__init__(forward_graph)

        # This allows to run simple check that ensures that
        # created graph have defined layer shape
        self.output_shape

    def clean_layer_references(self, layer_references):
        layers = []

        for layer_reference in layer_references:
            if isinstance(layer_reference, six.string_types):
                layer_reference = self.layer(layer_reference)
            layers.append(layer_reference)

        return layers

    def slice(self, directed_graph, layers):
        layers = self.clean_layer_references(layers)
        forward_graph = self.forward_graph

        if all(layer not in forward_graph for layer in layers):
            unused_layer = next(l for l in layers if l not in forward_graph)
            raise ValueError(
                "Layer `{}` is not used in the graph. Graph: {}, "
                "Layer: {}".format(unused_layer.name, self, unused_layer))

        observed_layers = []
        layers = copy.copy(layers)

        while layers:
            current_layer = layers.pop()
            observed_layers.append(current_layer)

            for next_layer in directed_graph[current_layer]:
                if next_layer not in observed_layers:
                    layers.append(next_layer)

        forward_subgraph = filter_graph(forward_graph, observed_layers)
        return self.__class__(forward_subgraph)

    def end(self, *output_layers):
        return self.slice(self.backward_graph, output_layers)

    def start(self, *input_layers):
        return self.slice(self.forward_graph, input_layers)

    @lazy_property
    def layers(self):
        return list(self)

    def layer(self, layer_name):
        if not isinstance(layer_name, six.string_types):
            raise ValueError(
                "Layer name expected to be a string, "
                "got value {}".format(layer_name))

        layers = []

        for layer in self.forward_graph:
            if layer.name == layer_name:
                layers.append(layer)

        if not layers:
            raise NameError(
                "Cannot find layer with name {!r}".format(layer_name))

        if len(layers) >= 2:
            raise NameError(
                "Ambiguous layer name `{}`. Network has {} "
                "layers with the same name. Layers: {}".format(
                    layer_name, len(layers), layers))

        return layers[0]

    @lazy_property
    def input_shapes(self):
        return [tf.TensorShape(l.input_shape) for l in self.input_layers]

    @lazy_property
    def input_shape(self):
        return make_one_if_possible(self.input_shapes)

    @lazy_property
    def output_shape(self):
        return self.get_output_shape(*self.input_shapes)

    @lazy_property
    def output_shapes_per_layer(self):
        return self.propagate_forward(
            copy.deepcopy(self.input_shapes),
            method='get_output_shape')

    def get_output_shape(self, *inputs):
        outputs = self.propagate_forward(
            copy.deepcopy(inputs),
            method='get_output_shape',
        )
        return make_one_if_possible(
            [outputs[l] for l in self.output_layers])

    def create_variables(self):
        output_shapes = self.output_shapes_per_layer
        backward_graph = self.backward_graph

        for layer in self:
            input_shapes = [layer.input_shape]
            from_layers = backward_graph[layer]

            if layer.frozen:
                continue

            if from_layers:
                input_shapes = [output_shapes[l] for l in from_layers]

            layer.create_variables(*input_shapes)
            layer.frozen = True

    def output(self, *inputs, **kwargs):
        self.create_variables()
        outputs = self.propagate_forward(inputs, method='output', **kwargs)
        return make_one_if_possible([outputs[l] for l in self.output_layers])

    def preformat_inputs(self, inputs):
        if len(inputs) == 1 and isinstance(inputs[0], dict):
            inputs = inputs[0]

        if not isinstance(inputs, dict):
            n_input_layers = len(self.input_layers)
            n_input_vars = len(inputs)

            if n_input_vars != n_input_layers:
                raise ValueError(
                    "Connection has {} input layer(s), but {} inputs was "
                    "provided".format(n_input_layers, n_input_vars))

            inputs = dict(zip(self.input_layers, inputs))

        prepared_inputs = {}
        for layer, input_variable in inputs.items():
            if isinstance(layer, six.string_types):
                layer = self.layer(layer)

            if layer not in self.forward_graph:
                raise ValueError(
                    "The `{}` layer doesn't appear in the network"
                    "".format(layer.name))

            if layer not in self.input_layers:
                raise ValueError(
                    "`{}` is not an input layer in the network"
                    "".format(layer.name))

            prepared_inputs[layer] = input_variable

        return prepared_inputs

    def pass_through_the_layer(self, layer, method, *args, **kwargs):
        layer_method = getattr(layer, method)

        try:
            return layer_method(*args, **kwargs)
        except Exception as exception:
            modified_exception = exception.__class__(
                "{original_message}. Exception occured while propagating data "
                "through the method `{method}`. Layer: {layer!r}".format(
                    original_message=str(exception).strip('.'),
                    method=method, layer=layer
                )
            )

            if hasattr(sys, 'last_traceback') and six.PY3:
                modified_exception = modified_exception.with_traceback(
                    sys.last_traceback)

            raise modified_exception

    def propagate_forward(self, inputs, method, **kwargs):
        backward_graph = self.backward_graph
        inputs = self.preformat_inputs(inputs)
        outputs = copy.copy(inputs)

        for layer, layer_input in inputs.items():
            outputs[layer] = self.pass_through_the_layer(
                layer, method, layer_input, **kwargs)

        for layer in (l for l in self if l not in inputs):
            layer_inputs = [outputs[l] for l in backward_graph[layer]]
            outputs[layer] = self.pass_through_the_layer(
                layer, method, *layer_inputs, **kwargs)

        return outputs

    @property
    def variables(self):
        self.create_variables()

        variables = OrderedDict()
        observed_variables = []

        for layer in self:
            for name, value in layer.variables.items():
                if value not in observed_variables:
                    observed_variables.append(value)
                    variables[(layer, name)] = value

        return variables

    @property
    def n_parameters(self):
        n_parameters = 0

        for variable in self.variables.values():
            n_parameters += variable.shape.num_elements()

        return n_parameters

    def predict(self, *inputs):
        session = tf_utils.tensorflow_session()
        feed_dict = dict(zip(as_tuple(self.inputs), inputs))
        return session.run(self.outputs, feed_dict=feed_dict)

    def is_sequential(self):
        if len(self.input_layers) > 1 or len(self.output_layers) > 1:
            return False

        forward_graph_layers = self.forward_graph.values()
        backward_graph_layers = self.backward_graph.values()

        for layers in chain(forward_graph_layers, backward_graph_layers):
            if len(layers) >= 2:
                # One of the layers has multiple input
                # or output networks
                return False

        return True

    def layer_names_only(self):
        prepared_graph = OrderedDict()

        for from_layer, to_layers in self.forward_graph.items():
            prepared_graph[from_layer.name] = [l.name for l in to_layers]

        return list(prepared_graph.items())

    def show(self, filepath=None):
        if filepath is None:
            filepath = tempfile.mktemp()

        def layer_uid(layer):
            return str(id(layer))

        digraph = graphviz.Digraph()
        shapes_per_layer = self.output_shapes_per_layer

        for layer in self.forward_graph.keys():
            digraph.node(layer_uid(layer), str(layer.name))

        output_id = 1
        for from_layer, to_layers in self.forward_graph.items():
            for to_layer in to_layers:
                digraph.edge(
                    layer_uid(from_layer),
                    layer_uid(to_layer),
                    label=" {}".format(shapes_per_layer[from_layer]))

            if not to_layers:
                output = 'output-{}'.format(output_id)

                digraph.node(output, 'Output #{}'.format(output_id))
                digraph.edge(
                    layer_uid(from_layer), output,
                    label=" {}".format(shapes_per_layer[from_layer]))

                output_id += 1

        digraph.render(filepath, view=True)

    def get_params(self):
        return {'forward_graph': self.forward_graph}

    def __contains__(self, entity):
        return entity in self.forward_graph

    def __len__(self):
        return len(self.forward_graph)

    def __iter__(self):
        for layer in topological_sort(self.backward_graph):
            yield layer

    def __repr__(self):
        if not self.forward_graph:
            return "[empty graph]"

        def format_shapes(shape):
            if isinstance(shape, tf.TensorShape):
                return str(shape)

            shapes = ', '.join([format_shapes(s) for s in shape])
            return '[' + shapes + ']'

        return '{} -> [... {} layers ...] -> {}'.format(
            format_shapes(self.input_shape),
            len(self),
            format_shapes(self.output_shape))


def validate_graphs_before_combining(left_graph, right_graph):
    left_out_layers = left_graph.output_layers
    right_in_layers = right_graph.input_layers

    if len(left_out_layers) > 1 and len(right_in_layers) > 1:
        raise LayerConnectionError(
            "Cannot make many to many connection between graphs. One graph "
            "has {n_left_outputs} outputs (layer names: {left_names}) and "
            "the other one has {n_right_inputs} inputs (layer names: "
            "{right_names}). First graph: {left_graph}, Second graph: "
            "{right_graph}".format(
                left_graph=left_graph,
                n_left_outputs=len(left_out_layers),
                left_names=[layer.name for layer in left_out_layers],

                right_graph=right_graph,
                n_right_inputs=len(right_in_layers),
                right_names=[layer.name for layer in right_in_layers],
            )
        )

    left_out_shapes = as_tuple(left_graph.output_shape)
    right_in_shapes = as_tuple(right_graph.input_shape)

    for left_layer, left_out_shape in zip(left_out_layers, left_out_shapes):
        right = zip(right_in_layers, right_in_shapes)

        for right_layer, right_in_shape in right:
            if left_out_shape.is_compatible_with(right_in_shape):
                continue

            raise LayerConnectionError(
                "Cannot connect layer `{left_name}` to layer `{right_name}`, "
                "because output shape ({left_out_shape}) of the first layer "
                "is incompatible with the input shape ({right_in_shape}) "
                "of the second layer. First layer: {left_layer}, Second "
                "layer: {right_layer}".format(
                    left_layer=left_layer,
                    left_name=left_layer.name,
                    left_out_shape=left_out_shape,

                    right_layer=right_layer,
                    right_name=right_layer.name,
                    right_in_shape=right_in_shape,
                )
            )


def merge(left_graph, right_graph, combine=False):
    if combine:
        validate_graphs_before_combining(left_graph, right_graph)

    forward_graph = OrderedDict()

    for key, value in left_graph.forward_graph.items():
        # To make sure that we copied lists inside of the
        # dictionary, but didn't copied values inside of the list
        forward_graph[key] = copy.copy(value)

    for key, values in right_graph.forward_graph.items():
        if key in forward_graph:
            for value in values:
                if value not in forward_graph[key]:
                    forward_graph[key].append(value)
        else:
            forward_graph[key] = copy.copy(values)

    if combine:
        for left_out_layer in left_graph.output_layers:
            for right_in_layer in right_graph.input_layers:
                forward_graph[left_out_layer].append(right_in_layer)

    if is_cyclic(forward_graph):
        raise LayerConnectionError(
            "Cannot define connection between layers, because it creates "
            "cycle in the graph. Left graph: {}, Right graph: {}"
            "".format(left_graph, right_graph))

    return LayerGraph(forward_graph)


def parallel(*networks):
    graph = LayerGraph()

    for network in networks:
        if isinstance(network, (list, tuple)):
            network = join(*network)
        graph = merge(graph, network)

    return graph


def join(*networks):
    graph = LayerGraph()

    for network in networks:
        graph = merge(graph, network, combine=True)

    return graph


def generate_layer_name(layer):
    if not hasattr(generate_layer_name, 'counters'):
        generate_layer_name.counters = defaultdict(int)

    classname = layer.__class__.__name__
    generate_layer_name.counters[classname] += 1
    layer_id = generate_layer_name.counters[classname]

    layer_name = re.sub(r'(?<!^)(?=[A-Z][a-z_])', '-', classname)
    return "{}-{}".format(layer_name.lower(), layer_id)


class BaseLayer(BaseGraph):
    """
    Base class for the layers.

    Parameters
    ----------
    name : str or None
        Layer's name. Can be used as a reference to specific layer. When
        value specified as ``None`` than name will be generated from
        the class name. Defaults to ``None``

    Methods
    -------
    variable(value, name, shape=None, trainable=True)
        Initializes variable with specified values.

    get_output_shape(input_shape)
        Computes expected output shape from the layer based on the
        specified input shape.

    output(*inputs, **kwargs)
        Propagetes input through the layer. The ``kwargs``  variable
        might contain additional information that propages through the
        network.

    Attributes
    ----------
    variables : dict
        Variable names and their values.
    """
    name = Property(expected_type=six.string_types)

    def __init__(self, name=None):
        # Layer by default gets intialized as a graph with single node in it
        super(BaseLayer, self).__init__(forward_graph=[(self, [])])

        if name is None:
            name = generate_layer_name(layer=self)

        self.variables = OrderedDict()
        self.name = name

        self._input_shape = tf.TensorShape(None)
        self.frozen = False

        # This decorator ensures that result produced by the
        # `output` method will be marked under layer's name scope.
        self.output = types.MethodType(
            tf_utils.class_method_name_scope(self.output), self)

    @property
    def input_shape(self):
        # Explicit TensorShape transformation not only ensures
        # that we have right type in the output, but also copies
        # value stored in the `_input_shape` in order to make sure
        # that no inplace update can effect original value
        return tf.TensorShape(self._input_shape)

    @input_shape.setter
    def input_shape(self, shape):
        if not self._input_shape.is_compatible_with(shape):
            raise ValueError(
                "Cannot update input shape of the layer, because it's "
                "incompatible with current input shape. Current shape: {}, "
                "New shape: {}, Layer: {}".format(
                    self._input_shape, shape, self))

        self._input_shape = shape

    @property
    def output_shape(self):
        return self.get_output_shape(self.input_shape)

    def get_output_shape(self, input_shape):
        return tf.TensorShape(None)

    def create_variables(self, *input_shapes):
        return NotImplemented

    def variable(self, value, name, shape=None, trainable=True):
        layer_name = 'layer/{layer_name}/{parameter_name}'.format(
            layer_name=self.name,
            parameter_name=name.replace('_', '-'))

        self.variables[name] = tf_utils.create_variable(
            value, layer_name, shape, trainable)

        return self.variables[name]

    def _repr_arguments(self, *args, **kwargs):
        def format_value(value):
            references = {
                'Variable': tf.Variable,
                'Array': np.ndarray,
                'Matrix': np.matrix,
            }

            for name, datatype in references.items():
                if isinstance(value, datatype):
                    return '<{} shape={}>'.format(name, value.shape)

            return repr(value)

        formatted_args = [str(arg) for arg in args]
        argspec = inspect.getargspec(self.__class__.__init__)

        def kwargs_priority(value):
            if value in argspec.args:
                return argspec.args.index(value)
            return float('inf')

        # Kwargs will have destroyed order of the arguments, and order in
        # the __init__ method allows to use proper order and validate names
        for name in sorted(kwargs.keys(), key=kwargs_priority):
            value = format_value(kwargs[name])
            formatted_args.append('{}={}'.format(name, value))

        return '{clsname}({formatted_args})'.format(
            clsname=self.__class__.__name__,
            formatted_args=', '.join(formatted_args))

    def __repr__(self):
        kwargs = {}

        for name in self.options:
            value = getattr(self, name)
            kwargs[name] = value

        return self._repr_arguments(**kwargs)


class Identity(BaseLayer):
    """
    Passes input through the layer without changes. Can be
    useful while defining residual networks in the network.

    Parameters
    ----------
    {BaseLayer.name}

    Methods
    -------
    {BaseLayer.Methods}

    Attributes
    ----------
    {BaseLayer.Attributes}
    """
    def get_output_shape(self, input_shape):
        return tf.TensorShape(input_shape)

    def output(self, input, **kwargs):
        return input


class Input(BaseLayer):
    """
    Layer defines network's input.

    Parameters
    ----------
    shape : int or tuple
        Shape of the input features per sample. Batch
        dimension has to be excluded from the shape.

    {BaseLayer.name}

    Methods
    -------
    {BaseLayer.Methods}

    Attributes
    ----------
    {BaseLayer.Attributes}

    Examples
    --------
    Feedforward Neural Network (FNN)

    In the example, input layer defines network that expects
    2D inputs (matrices). In other words, input to the network
    should be set of samples combined into matrix where each sample
    has 10 dimensional vector associated with it.

    >>> from neupy.layers import *
    >>> network = Input(10) >> Relu(5) >> Softmax(3)

    Convolutional Neural Network (CNN)

    In the example, input layer specified that we expect multiple
    28x28 image as an input and each image should have single
    channel (images with no color).

    >>> from neupy.layers import *
    >>> network = join(
    ...     Input((28, 28, 1)),
    ...     Convolution((3, 3, 16)) >> Relu(),
    ...     Convolution((3, 3, 16)) >> Relu(),
    ...     Reshape()
    ...     Softmax(10),
    ... )
    """
    shape = TypedListProperty(element_type=(int, type(None)))

    def __init__(self, shape, name=None):
        super(Input, self).__init__(name=name)

        if isinstance(shape, tf.TensorShape):
            shape = tf_utils.shape_to_tuple(shape)

        self.shape = as_tuple(shape)

    @property
    def input_shape(self):
        batch_shape = tf.TensorShape([None])
        return batch_shape.concatenate(self.shape)

    def output(self, input, **kwargs):
        return input

    def get_output_shape(self, input_shape):
        if not self.input_shape.is_compatible_with(input_shape):
            raise LayerConnectionError(
                "Input layer got unexpected input shape. "
                "Received shape: {}, Expected shape: {}"
                "".format(input_shape, self.input_shape)
            )
        return self.input_shape.merge_with(input_shape)

    def __repr__(self):
        return self._repr_arguments(
            make_one_if_possible(self.shape),
            name=self.name)
