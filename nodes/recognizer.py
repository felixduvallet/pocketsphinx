#!/usr/bin/env python

"""
recognizer.py is a wrapper for pocketsphinx.
  parameters:
    ~lm - filename of language model
    ~dict - filename of dictionary
    ~mic_name - set the pulsesrc device name for the microphone input.
           e.g. a Logitech G35 Headset has the following device name:
           alsa_input.usb-Logitech_Logitech_G35_Headset-00-Headset_1.analog-mono
           To list audio device info on your machine, in a terminal type:
           pacmd list-sources
  publications:
    ~output (std_msgs/String) - text output
  services:
    ~start (std_srvs/Empty) - start speech recognition
    ~stop (std_srvs/Empty) - stop speech recognition
"""

import roslib; roslib.load_manifest('pocketsphinx')
import rospy
import os

import pygtk
pygtk.require('2.0')
import gtk

import gobject
import pygst
pygst.require('0.10')
gobject.threads_init()
import gst

from std_msgs.msg import String
from std_srvs.srv import Empty, EmptyResponse
from audio_common_msgs.msg import AudioData

import commands

class recognizer(object):
    """ GStreamer based speech recognizer. """

    def __init__(self):
        # Start node
        rospy.init_node("recognizer")

        # Find the name of your microphone by typing pacmd list-sources in the
        # terminal.
        self._device_name_param = "~mic_name"
        self._lm_param = "~lm"
        self._dic_param = "~dict"
        self._audio_topic_param = "~audio_msg_topic"

        self.asr = None
        self.bus = None
        self.bus_id = None

        # Audio ROS topic, if this is set the recognizer will subscribe to
        # AudioData messages on this topic.
        self._ros_audio_topic = None
        self._app_source = None  # The gstreamer appsrc element

        # Configure mics with gstreamer launch config
        if rospy.has_param(self._device_name_param):
            self.device_name = rospy.get_param(self._device_name_param)
            self.device_index = self.pulse_index_from_name(self.device_name)
            self.launch_config = "pulsesrc device=" + str(self.device_index)
            rospy.loginfo("Using: pulsesrc device=%s name=%s", self.device_index, self.device_name)
        elif rospy.has_param('~source'):
            # common sources: 'alsasrc'
            self.launch_config = rospy.get_param('~source')
        elif rospy.has_param(self._audio_topic_param):
            # Use ROS audio messages as input: Use an appsrc to pass AudioData
            # messages to the gstreamer pipeline. Use 'mad' plugin to decode
            # mp3-formatted messages.
            self.launch_config = 'appsrc name=appsrc ! mad'
            self._ros_audio_topic = rospy.get_param(self._audio_topic_param)
            rospy.loginfo('Using ROS audio messages as input. Topic: {}'.
                          format(self._ros_audio_topic))
        else:
            self.launch_config = 'gconfaudiosrc'

        rospy.loginfo("Audio input: {}".format(self.launch_config))

        self.launch_config += " ! audioconvert ! audioresample " \
                            + '! vader name=vad auto-threshold=true ' \
                            + '! pocketsphinx name=asr ! fakesink'

        # Configure ROS settings
        self.started = False
        rospy.on_shutdown(self.shutdown)
        self.pub = rospy.Publisher('~output', String, queue_size=10)
        rospy.Service("~start", Empty, self.start)
        rospy.Service("~stop", Empty, self.stop)

        if rospy.has_param(self._lm_param) and rospy.has_param(self._dic_param):
            self.start_recognizer()
        else:
            rospy.logwarn("lm and dic parameters need to be "
                          "set to start recognizer.")

    def start_recognizer(self):
        rospy.loginfo('Starting recognizer... pipeline: {}'.format(
            self.launch_config))
        self.pipeline = gst.parse_launch(self.launch_config)
        if not self.pipeline:
            rospy.logerr('Could not create gstreamer pipeline.')
            return
        rospy.loginfo('gstreamer pipeline created.')

        self.asr = self.pipeline.get_by_name('asr')
        self.asr.connect('partial_result', self.asr_partial_result)
        self.asr.connect('result', self.asr_result)
        self.asr.set_property('configured', True)
        self.asr.set_property('dsratio', 1)

        # If the ros audio topic exists, we subscribe to AudioData messages.
        # Also make sure the appsource element was created properly.
        if self._ros_audio_topic:
            self._app_source = self.pipeline.get_by_name('appsrc')
            rospy.loginfo('Subscribing to AudioData on topic: {}'.format(
                self._ros_audio_topic))
            rospy.Subscriber(
                self._ros_audio_topic, AudioData, self.on_audio_message)
            if not self._app_source:
                rospy.logerr('Error getting the appsrc element.')
                return

        # Configure language model
        if rospy.has_param(self._lm_param):
            lm = rospy.get_param(self._lm_param)
            if not os.path.isfile(lm):
                rospy.logerr(
                    'Language model file does not exist: {}'.format(lm))
                return
        else:
            rospy.logerr('Recognizer not started. Please specify a '
                         'language model file.')
            return

        if rospy.has_param(self._dic_param):
            dic = rospy.get_param(self._dic_param)
            if not os.path.isfile(dic):
                rospy.logerr(
                    'Dictionary file does not exist: {}'.format(dic))
                return
        else:
            rospy.logerr('Recognizer not started. Please specify a dictionary.')
            return

        self.asr.set_property('lm', lm)
        self.asr.set_property('dict', dic)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus_id = self.bus.connect('message::application',
                                       self.application_message)
        self.pipeline.set_state(gst.STATE_PLAYING)
        self.started = True

        rospy.loginfo('Recognizer started!')

    def pulse_index_from_name(self, name):
        output = commands.getstatusoutput(
            ("pacmd list-sources | grep -B 1 'name: <" + name +
             ">' | grep -o -P '(?<=index: )[0-9]*'"))

        if len(output) == 2:
            return output[1]
        else:
            raise Exception("Error. pulse index doesn't exist for name: {}".
                            format(name))

    def stop_recognizer(self):
        if self.started:
            self.pipeline.set_state(gst.STATE_NULL)
            self.pipeline.remove(self.asr)
            self.bus.disconnect(self.bus_id)
            self.started = False

    def shutdown(self):
        """ Delete any remaining parameters so they don't affect next launch """
        for param in [self._device_name_param, self._lm_param, self._dic_param,
                      self._audio_topic_param]:
            if rospy.has_param(param):
                rospy.delete_param(param)

        """ Shutdown the GTK thread. """
        gtk.main_quit()

    def start(self, req):
        self.start_recognizer()
        rospy.loginfo("recognizer started")
        return EmptyResponse()

    def stop(self, req):
        self.stop_recognizer()
        rospy.loginfo("recognizer stopped")
        return EmptyResponse()

    def asr_partial_result(self, asr, text, uttid):
        """ Forward partial result signals on the bus to the main thread. """
        struct = gst.Structure('partial_result')
        struct.set_value('hyp', text)
        struct.set_value('uttid', uttid)
        asr.post_message(gst.message_new_application(asr, struct))

    def asr_result(self, asr, text, uttid):
        """ Forward result signals on the bus to the main thread. """
        struct = gst.Structure('result')
        struct.set_value('hyp', text)
        struct.set_value('uttid', uttid)
        asr.post_message(gst.message_new_application(asr, struct))

    def application_message(self, bus, msg):
        """ Receive application messages from the bus. """
        msgtype = msg.structure.get_name()
        if msgtype == 'partial_result':
            self.partial_result(msg.structure['hyp'], msg.structure['uttid'])
        if msgtype == 'result':
            self.final_result(msg.structure['hyp'], msg.structure['uttid'])

    def partial_result(self, hyp, uttid):
        """ Delete any previous selection, insert text and select it. """
        rospy.logdebug("Partial: " + hyp)

    def final_result(self, hyp, uttid):
        """ Insert the final result. """
        msg = String()
        msg.data = str(hyp.lower())
        rospy.loginfo('Final result: {}'.format(msg.data))
        self.pub.publish(msg)

    def on_audio_message(self, audio):
        # Callback for ROS audio messages -- emits the audio data to the
        # gstreamer pipeline through the appsrc.
        rospy.logdebug('Received audio packet of length {}'.format(
            len(audio.data)))

        if self._app_source:
            self._app_source.emit('push-buffer',
                                  gst.Buffer(str(bytearray(audio.data))))


if __name__ == "__main__":
    start = recognizer()
    gtk.main()
