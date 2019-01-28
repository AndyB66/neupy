import pytest
import numpy as np
import tensorflow as tf
from tensorflow.errors import FailedPreconditionError

from neupy import init
from neupy.utils import tf_utils, asfloat

from base import BaseTestCase


@pytest.mark.parametrize("in_shape,out_shape", [
    ((10,), (10,)),
    ((10, 2), (20,)),
    ((10, 2, 4), (80,)),
])
def test_flatten(in_shape, out_shape):
    X = np.random.random(in_shape)
    Y = tf_utils.tensorflow_eval(tf_utils.flatten(X))
    assert Y.shape == out_shape


class TFUtilsTestCase(BaseTestCase):
    def test_outer(self):
        actual = self.eval(tf_utils.outer(np.ones(10), np.ones(10)))
        np.testing.assert_array_almost_equal(actual, np.ones((10, 10)))

    def test_dot(self):
        actual = self.eval(tf_utils.dot(
            np.arange(10).astype(np.float32),
            2 * np.arange(10).astype(np.float32),
        ))
        self.assertEqual(actual, 570)

    def test_repeat(self):
        matrix = np.array([
            [1, 2],
            [3, 4],
        ])
        actual = self.eval(tf_utils.repeat(matrix, (2, 3)))
        expected = np.array([
            [1, 1, 1, 2, 2, 2],
            [1, 1, 1, 2, 2, 2],
            [3, 3, 3, 4, 4, 4],
            [3, 3, 3, 4, 4, 4],
        ])
        np.testing.assert_array_equal(actual, expected)

    def test_make_single_vector(self):
        w1 = tf.Variable(np.ones((4, 3)))
        b1 = tf.Variable(np.zeros((3,)))
        w2 = tf.Variable(np.ones((3, 2)))

        actual = self.eval(tf_utils.make_single_vector([w1, b1, w2]))
        expected = np.array([1] * 12 + [0] * 3 + [1] * 6)

        np.testing.assert_array_equal(actual, expected)

    def test_setup_parameter_updates(self):
        w1 = tf.Variable(np.ones((4, 3)))
        b1 = tf.Variable(np.zeros((3,)))
        w2 = tf.Variable(np.ones((3, 2)))

        tf_utils.initialize_uninitialized_variables([w1, b1, w2])

        updates = 2 * tf_utils.make_single_vector([w1, b1, w2]) + 1
        updates = tf_utils.setup_parameter_updates([w1, b1, w2], updates)

        sess = tf_utils.tensorflow_session()
        for parameter, new_value in updates:
            sess.run(parameter.assign(new_value))

        np.testing.assert_array_almost_equal(
            self.eval(w1),
            3 * np.ones((4, 3)),
        )
        np.testing.assert_array_almost_equal(
            self.eval(b1),
            np.ones(3),
        )
        np.testing.assert_array_almost_equal(
            self.eval(w2),
            3 * np.ones((3, 2)),
        )

    def test_function_name_scope(self):
        @tf_utils.function_name_scope
        def new_variable():
            return tf.Variable(0, name='myvar')

        variable = new_variable()
        self.assertEqual(variable.name, 'new_variable/myvar:0')

    def test_class_method_name_scope(self):
        class MyRelu(object):
            @tf_utils.class_method_name_scope
            def output(self):
                return tf.Variable(0, name='weights')

        variable = MyRelu().output()
        self.assertEqual(variable.name, 'MyRelu/weights:0')

    def test_function_without_updates(self):
        x = tf.placeholder(name='x', dtype=tf.float32)
        w = tf.Variable(asfloat(np.random.random((4, 3))), name='w')
        b = tf.Variable(asfloat(np.random.random((3,))), name='b')
        y = tf.matmul(x, w) + b

        prediction = tf_utils.function([x], y)
        tf_utils.initialize_uninitialized_variables()

        actual = prediction(np.random.random((7, 4)))
        self.assertEqual(actual.shape, (7, 3))

    def test_function_with_updates(self):
        x = tf.placeholder(name='x', dtype=tf.float32)
        w = tf.Variable(asfloat(np.ones((4, 3))), name='w')
        b = tf.Variable(asfloat(np.ones((3,))), name='b')
        y = tf.matmul(x, w) + b

        prediction = tf_utils.function([x], y, updates=[
            (b, b - 0.5),
            w.assign(w + 0.5),
        ])
        tf_utils.initialize_uninitialized_variables()

        actual = prediction(np.random.random((7, 4)))
        self.assertEqual(actual.shape, (7, 3))

        np.testing.assert_array_almost_equal(
            self.eval(w),
            1.5 * np.ones((4, 3)),
        )
        np.testing.assert_array_almost_equal(
            self.eval(b),
            0.5 * np.ones((3,)),
        )

    def test_tensorflow_session_function(self):
        sess_a = tf_utils.tensorflow_session()
        sess_b = tf_utils.tensorflow_session()
        self.assertIs(sess_a, sess_b)

        sess_b.close()
        sess_c = tf_utils.tensorflow_session()
        self.assertIsNot(sess_b, sess_c)

    def test_initialize_uninitialized_variables(self):
        sess = tf_utils.tensorflow_session()

        a = tf.Variable(np.ones((4, 3)), name='a')
        b = tf.Variable(np.ones((4, 3)), name='b')
        tf_utils.initialize_uninitialized_variables()
        actual = sess.run(a + b)
        np.testing.assert_array_almost_equal(actual, 2 * np.ones((4, 3)))

        c = tf.Variable(np.ones((2, 3)), name='c')
        d = tf.Variable(np.ones((2, 3)), name='dx')
        tf_utils.initialize_uninitialized_variables([c])

        with self.assertRaisesRegexp(FailedPreconditionError, "value dx"):
            sess.run(c + d)

    def test_variable_creation(self):
        weight = np.ones((3, 3))
        var1 = tf_utils.create_variable(weight, name='var1', shape=(3, 3))
        self.assertShapesEqual(var1.shape, (3, 3))

        var2 = tf_utils.create_variable(5, name='var2', shape=(4, 3))
        self.assertShapesEqual(var2.shape, (4, 3))
        np.testing.assert_array_almost_equal(
            self.eval(var2), 5 * np.ones((4, 3)))

        initializer = init.Normal()
        var3 = tf_utils.create_variable(initializer, name='var3', shape=(4, 7))
        self.assertShapesEqual(var3.shape, (4, 7))

        weight = tf.Variable(np.ones((3, 3)), dtype=tf.float32)
        var4 = tf_utils.create_variable(weight, name='var4', shape=(3, 3))
        self.assertShapesEqual(var4.shape, (3, 3))
        self.assertIs(var4, weight)

        weight = np.ones((3, 4))
        with self.assertRaisesRegexp(ValueError, "Cannot create variable"):
            tf_utils.create_variable(weight, name='var5', shape=(3, 3))

        weight = tf.Variable(np.ones((4, 3)), dtype=tf.float32)
        with self.assertRaisesRegexp(ValueError, "Cannot create variable"):
            tf_utils.create_variable(weight, name='var6', shape=(3, 3))
