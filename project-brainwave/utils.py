def normalize_and_rgb(images): 
    import numpy as np
    #normalize image to 0-255 per image.
    image_sum = 1/np.sum(np.sum(images,axis=1),axis=-1)
    given_axis = 0
    # Create an array which would be used to reshape 1D array, b to have 
    # singleton dimensions except for the given axis where we would put -1 
    # signifying to use the entire length of elements along that axis  
    dim_array = np.ones((1,images.ndim),int).ravel()
    dim_array[given_axis] = -1
    # Reshape b with dim_array and perform elementwise multiplication with 
    # broadcasting along the singleton dimensions for the final output
    image_sum_reshaped = image_sum.reshape(dim_array)
    images = images*image_sum_reshaped*255

    # make it rgb by duplicating 3 channels.
    images = np.stack([images, images, images],axis=-1)
    
    return images

def image_with_label(train_file, istart,iend):
    import tables
    import numpy as np
    f = tables.open_file(train_file, 'r')
    a = np.array(f.root.img_pt) # Images
    b = np.array(f.root.label) # Labels
    return normalize_and_rgb(a[istart:iend]),b[istart:iend]

def count_events(train_files):
    import tables
    n_events = 0
    for train_file in train_files:
        f = tables.open_file(train_file, 'r')
        n_events += f.root.label.shape[0]
    return n_events

def preprocess_images():
    import tensorflow as tf
    # Create a placeholder for our incoming images
    in_images = tf.placeholder(tf.float32)
    in_height = 64
    in_width = 64
    in_images.set_shape([None, in_height, in_width, 3])
    
    # Resize those images to fit our featurizer
    out_width = 224
    out_height = 224
    image_tensors = tf.image.resize_images(in_images, [out_height,out_width])
    image_tensors = tf.to_float(image_tensors)
    
    return in_images, image_tensors

def construct_classifier():
    from keras.layers import Dropout, Dense, Flatten, Input
    from keras.models import Model
    from keras import backend as K
    import tensorflow as tf
    K.set_session(tf.get_default_session())
    
    FC_SIZE = 1024
    NUM_CLASSES = 2

    in_layer = Input(shape=(1, 1, 2048,),name='input_1')
    x = Dropout(0.2, input_shape=(1, 1, 2048,),name='dropout_1')(in_layer)
    x = Dense(FC_SIZE, activation='relu', input_dim=(1, 1, 2048,),name='dense_1')(x)
    x = Flatten(name='flatten_1')(x)
    preds = Dense(NUM_CLASSES, activation='softmax', input_dim=FC_SIZE, name='classifier_output')(x)
    
    model = Model(inputs = in_layer, outputs = preds)
    
    return model

def construct_model(quantized, saved_model_dir = None, starting_weights_directory = None):
    from azureml.contrib.brainwave.models import Resnet50, QuantizedResnet50
    import tensorflow as tf
    from keras import backend as K
    
    # Convert images to 3D tensors [width,height,channel]
    in_images, image_tensors = preprocess_images()

    # Construct featurizer using quantized or unquantized ResNet50 model
    if not quantized:
        featurizer = Resnet50(saved_model_dir, custom_weights_directory = starting_weights_directory)
    else:
        featurizer = QuantizedResnet50(saved_model_dir, custom_weights_directory = starting_weights_directory)


    features = featurizer.import_graph_def(input_tensor=image_tensors)
    
    # Construct classifier
    classifier = construct_classifier()
    preds = classifier(features)
    
    # Initialize weights
    sess = tf.get_default_session()
    tf.global_variables_initializer().run()
    
    
    featurizer.restore_weights(sess)
    
    if starting_weights_directory is not None:
        print("loading classifier weights from", starting_weights_directory+'/class_weights.h5')
        classifier.load_weights(starting_weights_directory+'/class_weights.h5')
        
    return in_images, image_tensors, features, preds, featurizer, classifier 

def check_model(preds, in_images, train_files, classifier):
    import tensorflow as tf
    from keras import backend as K
    
    sess = tf.get_default_session()
    in_labels = tf.placeholder(tf.float32, shape=(None, 2))
    a, b = image_with_label(train_files[0],0,1)
    c = classifier.layers[-1].weights[0]
    d = classifier.layers[-1].weights[1]
    print(" image:    ", a)
    print(" label:    ", b)
    print(" features: ", sess.run(features, feed_dict={in_images: a,
                                   in_labels: b,
                                   K.learning_phase(): 0}))
    print(" weights:  ", sess.run(c))
    print(" biases:   ", sess.run(d))    
    print(" preds:    ", sess.run(preds, feed_dict={in_images: a,
                                   in_labels: b,
                                   K.learning_phase(): 0}))
    
def chunks(files, chunksize): 
    """Yield successive n-sized chunks from a and b.""" 
    import tables
    import numpy as np
    for train_file in files: 
        f = tables.open_file(train_file, 'r') 
        a = np.array(f.root.img_pt) # Images 
        b = np.array(f.root.label) # Labels 
        c = np.c_[a.reshape(len(a), -1), b.reshape(len(b), -1)]
        np.random.shuffle(c)
        test_images = c[:, :a.size//len(a)].reshape(a.shape)
        test_labels = c[:, a.size//len(a):].reshape(b.shape)

        for istart in range(0,test_images.shape[0],chunksize): 
            yield normalize_and_rgb(test_images[istart:istart+chunksize]),test_labels[istart:istart+chunksize]

def train_model(preds, in_images, train_files, is_retrain = False, train_epoch = 10, classifier=None, saver=None, checkpoint_path=None): 
    """ training model """ 
    import tensorflow as tf
    from keras import backend as K
    from keras.objectives import binary_crossentropy 
    from keras.metrics import categorical_accuracy 
    from tqdm import tqdm

    learning_rate = 0.001 if is_retrain else 0.01

    # Specify the loss function
    in_labels = tf.placeholder(tf.float32, shape=(None, 2))   
    cross_entropy = tf.reduce_mean(binary_crossentropy(in_labels, preds))
    optimizer = tf.train.GradientDescentOptimizer(learning_rate).minimize(cross_entropy)

    accuracy = tf.reduce_mean(categorical_accuracy(in_labels, preds))
    auc = tf.metrics.auc(tf.cast(in_labels, tf.bool), preds)
    
    sess = tf.get_default_session()
    sess.run(tf.local_variables_initializer())
    
    # Create a summary to monitor cross_entropy loss
    tf.summary.scalar("loss", cross_entropy)
    # Create a summary to monitor accuracy 
    tf.summary.scalar("accuracy", accuracy)
    # Create a summary to monitor auc tensor
    tf.summary.scalar("auc", auc[0])
    
    # Create summaries to visualize weights
    #for var in tf.trainable_variables():
    #    tf.summary.histogram(var.name.replace(':','_'), var)
        
    #grads = tf.gradients(cross_entropy, tf.trainable_variables())
    #grads = list(zip(grads, tf.trainable_variables()))
    
    # Summarize all gradients
    #for grad, var in grads:
    #    tf.summary.histogram(var.name.replace(':','_') + '/gradient', grad)

    # Merge all summaries into a single op
    merged_summary_op = tf.summary.merge_all()

    chunk_size = 64
    n_train_events = count_events(train_files)
    chunk_num = n_train_events / chunk_size
    
    summary_writer = tf.summary.FileWriter(checkpoint_path + '/logs', sess.graph)

    loss_over_epoch = []
    accuracy_over_epoch = []
    auc_over_epoch = []

    for epoch in range(train_epoch):
        avg_loss = 0
        avg_accuracy = 0
        avg_auc = 0
        preds_temp = []
        label_temp = []
        i = 0
        for img_chunk, label_chunk in tqdm(chunks(train_files, chunk_size),total=chunk_num):
            _, loss, summary = sess.run([optimizer, cross_entropy, merged_summary_op],
                            feed_dict={in_images: img_chunk,
                                       in_labels: label_chunk,
                                       K.learning_phase(): 1})
            avg_loss += loss / chunk_num
            accuracy_result, auc_result, preds_result = sess.run([accuracy, auc, preds],
                                  feed_dict={in_images: img_chunk,
                                            in_labels: label_chunk,
                                            K.learning_phase(): 0})
            avg_accuracy += accuracy_result / chunk_num
            avg_auc += auc_result[0] / chunk_num
            summary_writer.add_summary(summary, epoch * chunk_num + i)
            i += 1
        
        print("Epoch:", (epoch + 1), "loss = ", "{:.3f}".format(avg_loss))
        print("Training Accuracy:", "{:.3f}".format(avg_accuracy), ", Area under ROC curve:", "{:.3f}".format(avg_auc))

        loss_over_epoch.append(avg_loss)
        accuracy_over_epoch.append(avg_accuracy)
        auc_over_epoch.append(avg_auc)

        if saver is not None and checkpoint_path is not None and classifier is not None:
            saver.save(sess, checkpoint_path+'/resnet50_bw', write_meta_graph=False, global_step = epoch)
            saver.save(sess, checkpoint_path+'/resnet50_bw', write_meta_graph=False)
            classifier.save_weights(checkpoint_path+'/class_weights-%s.h5'%epoch)
            classifier.save(checkpoint_path+'/class_model-%s.h5'%epoch)
            classifier.save_weights(checkpoint_path+'/class_weights.h5')
            classifier.save(checkpoint_path+'/class_model.h5')
        
    return loss_over_epoch, accuracy_over_epoch, auc_over_epoch

def test_model(preds, in_images, test_files):
    """Test the model"""
    import tensorflow as tf
    from keras import backend as K
    import numpy as np
    from keras.metrics import categorical_accuracy
    from tqdm import tqdm
    
    in_labels = tf.placeholder(tf.float32, shape=(None, 2))
    accuracy = tf.reduce_mean(categorical_accuracy(in_labels, preds))
    auc = tf.metrics.auc(tf.cast(in_labels, tf.bool), preds)
   
    chunk_size = 64
    n_test_events = count_events(test_files)
    chunk_num = n_test_events/chunk_size
    preds_all = []
    label_all = []
    
    sess = tf.get_default_session()
    sess.run(tf.local_variables_initializer())
    
    avg_accuracy = 0
    avg_auc = 0
    for img_chunk, label_chunk in tqdm(chunks(test_files, chunk_size),total=chunk_num):
        accuracy_result, auc_result, preds_result = sess.run([accuracy, auc, preds],
                        feed_dict={in_images: img_chunk,
                                   in_labels: label_chunk,
                                   K.learning_phase(): 0})
        avg_accuracy += accuracy_result / chunk_num
        avg_auc += auc_result[0] / chunk_num 
        preds_all.extend(preds_result)
        label_all.extend(label_chunk)
            
    return avg_accuracy, avg_auc, np.asarray(preds_all).reshape(n_test_events,2), np.asarray(label_all).reshape(n_test_events,2)