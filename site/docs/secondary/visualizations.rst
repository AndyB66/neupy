Visualizations
==============

Hinton diagram
--------------

More information about the Hinton diagram you can find in :plot:`documentation <hinton>`. The small example is shown below.

.. code-block:: python

    import numpy as np
    import matplotlib.pyplot as plt
    from neupy import plots

    weight = np.random.randn(20, 20)

    plt.style.use('ggplot')
    plt.figure(figsize=(16, 12))
    plt.title("Hinton diagram")
    plots.hinton(weight)
    plt.show()

.. figure:: images/plots-hinton-example.png
    :width: 100%
    :align: center
    :alt: Hinton diagram example from NeuPy library

Error plot
----------

TODO: Add documentation


Network structure
-----------------

Relations between layers in the network can be visualized using the ``network_structure`` function.

.. code-block:: python

    from neupy import layers, plots

    network = layers.join(
        layers.Input(10),
        layers.Relu(20),
        layers.Softmax(4),
    )
    plots.network_structure(network)

.. raw:: html

    <br>

.. image:: images/layer-structure-plot.png
    :width: 70%
    :align: center
    :alt: Layer structure plot example

Saliency Map
------------

Read more in the :plot:`Saliency Map <saliency_map>` function documentation.

.. code-block:: python

    from neupy import plots

    vgg19 = ...  # define pretrained VGG19 network
    dog_image = ...  # load image of dog

    # apply preprocessing step to dog image
    processed_dog_image = process(dog_image)

    plt.imshow(dog_image)
    plots.saliency_map(vgg19, processed_dog_image, alpha=0.6, sigma=10)

.. raw:: html

    <br>

.. image:: images/saliency-map-plot.png
    :width: 50%
    :align: center
    :alt: Saliency Map
