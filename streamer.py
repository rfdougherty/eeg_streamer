import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui
import numpy as np
import pandas as pd
import json
from datetime import datetime
from threading import Thread,Event
from queue import Queue
import logging
from collections import OrderedDict
import os


from dsi_api import *

# TODO:
# * catch ctrl-c can properly shutdown
# * add text box to enter subject code
# * Add buttons for:
#   * toggle impedance mode
#   * pause recording
#   * start recording
#   * end session (pauses recording, powers off headset, closes windows, exits)
#   * reset amps
# Need a way to tag sessions? E.g., VEP, finger tapping, phone use, etc.?

# 13579#

simulate = False
do_impedance = False

sensors = ['Fp1','Fp2','F7','F3','Fz','F4','F8','A1','T3','C3','Cz','C4','T4','A2','T5','P3','Pz','P4','T6','O1','O2','TRG','X1'] # 'X2','X3'
imp_sensors = sensors[:21]
max_viz_samples = 500
max_imp_samples = 100
numPlots_per_row = 2

logging.basicConfig(filename='/tmp/eeg.log', level=logging.DEBUG)

signal_q = Queue()
impedance_q = Queue()
record = False
outdir = '/tmp/'

stop_event = Event()
paused_event = Event()
impedance_event = Event()
signal_event = Event()
record_event = Event()
subid = 'TEST'

sigdf = pd.DataFrame(columns=sensors, data=np.zeros((max_viz_samples, len(sensors))))
impdf = pd.DataFrame(columns=sensors, data=np.zeros((max_imp_samples, len(sensors))))


@MessageCallback
def msg_callback( msg, lvl=0 ):
    if lvl <= 3:  # ignore messages at debugging levels higher than 3
        logging.warning('DSI Message (level %d): %s' % (lvl, msg) )
    return 1

@SampleCallback
def sample_callback_signals( headsetPtr, packetTime, userData ):
    h = Headset( headsetPtr )
    #samp = {'ts':packetTime, 'userdata':userData, 'data': {ch.GetName().decode():ch.ReadBuffered() for ch in h.Channels() }}
    samp = {ch.GetName().decode():ch.ReadBuffered() for ch in h.Channels()}
    samp.update({'timestamp':packetTime})
    signal_q.put(samp)
    #if record_event.set():
    #    if not os.path.exists(outfile):
    #        #new_df.to_csv(outfile, index=False)
    #        with open(outfile, 'w') as fp:
    #            fp.write(json.dumps(samp) + '\n')
    #    else:
    #        #new_df.to_csv(outfile, mode='a', index=False, header=False)
    #        with open(outfile, 'a') as fp:
    #            fp.write(json.dumps(samp) + '\n')


@SampleCallback
def sample_callback_impedances( headsetPtr, packetTime, userData ):
    h = Headset( headsetPtr )
    samp = {'ts':packetTime, 'userdata':userData, 'ref': h.GetFactoryReferenceString(), 'cmf': h.GetImpedanceCMF(),
            'data': {src.GetName().decode():src.GetImpedanceEEG() for src in h.Sources() if src.IsReferentialEEG() and not src.IsFactoryReference()}}
    impedance_q.put(samp)
    #samp = {'ts':packetTime, 'userdata':userData, 'data': {ch.GetName().decode():ch.ReadBuffered() for ch in h.Channels() }}
    samp = {ch.GetName().decode():ch.ReadBuffered() for ch in h.Channels()}
    samp.update({'timestamp':packetTime})
    signal_q.put(samp)
    # imps = [{'ts':s['ts'], 'cmf':s['cmf'], 'ref':s['ref'], 'data':{k.decode():s['data'][k] for k in s['data']}} for s in impedance_q.queue]


class DataAcquisitionThread(Thread):

    def __init__(self, signal_q, impedance_q, stop_event, paused_event, impedance_event, signal_event, port=None, reference=None):
        super(DataAcquisitionThread, self).__init__()
        if port == None:
            from glob import glob
            ports = glob('/dev/tty.DSI*')
            if len(ports) >= 1:
                if len(ports) > 1:
                    logging.warn('%d ports found! Using the first.' % len(ports))
                logging.info('Using port %s...' % ports[0])
                self.port = ports[0].encode()
            else:
                logging.warn('No ports found!')
                self.port = None
        self.reference = reference
        self.signal_q = signal_q
        self.impedance_q = impedance_q
        self.headset = None
        self.stop_event = stop_event #Event()
        self.paused_event = paused_event #Event()
        self.impedance_event = impedance_event #Event()
        self.signal_event = signal_event #Event()

    def connect_headset(self):
        self.headset = Headset()
        self.headset.SetMessageCallback(msg_callback)
        self.headset.Connect(self.port)

    def setup_impedance(self):
        self.headset.StartAnalogReset()
        self.headset.SetSampleCallback(sample_callback_impedances, 0)
        self.headset.StartImpedanceDriver()

    def setup_signal(self):
        self.headset.StopImpedanceDriver()
        self.headset.StartAnalogReset()
        self.headset.SetSampleCallback(sample_callback_signals, 0)
        if self.reference != None:
            self.headset.SetDefaultReference(self.reference, True)

    def reset_amps(self):
        self.headset.StartAnalogReset()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def run(self):
        while not self.stop_event.is_set():
            if not self.paused_event.is_set():
                if self.signal_event.is_set():
                    self.headset.StopDataAcquisition()
                    self.setup_signal()
                    self.headset.StartDataAcquisition()
                    self.signal_event.clear()
                elif self.impedance_event.is_set():
                    self.headset.StopDataAcquisition()
                    self.setup_impedance()
                    self.headset.StartDataAcquisition()
                    self.impedance_event.clear()
                #self.headset.Receive(.1, 0) # (seconds, idleAfterSeconds)
                self.headset.Idle(0.0)
                # RECORD HERE?
            else:
                print('PAUSED')
                #self.paused_event.clear()

            # TODO: save data stream to disk.
            # Add a thread to process data from the signal_q-- save it to disk and update the visualization dataframe.
            # Or just stream to disk in the signal callback?
        return

    def stop(self):
        self.stop_event.set()

    def impedance_mode(self):
        self.signal_event.clear()
        self.impedance_event.set()

    def signal_mode(self):
        self.impedance_event.clear()
        self.signal_event.clear()

def update_signals():
    global signal_q, sigdf, sensors, siglines

    if simulate:
        #new_data = [{s:v for s,v in zip(sensors, np.random.randn(len(sensors))*50)} for i in range(10)]
        new_data = [OrderedDict([('timestamp',i)] + [(s,v) for s,v in zip(sensors, np.random.randn(len(sensors))*50)]) for i in range(10)]
    else:
        new_data = []
        while signal_q.not_empty:
            try:
                sample = signal_q.get_nowait()
                new_data.append(sample)
            except:
                break

    #new_df = pd.DataFrame(new_data[-max_viz_samples:])
    new_df = pd.DataFrame(new_data)
    if True: #record_event.set():
        outfile = outdir + subid + '.csv'
        if not os.path.exists(outfile):
            new_df.to_csv(outfile, index=False)
        else:
            new_df.to_csv(outfile, mode='a', index=False, header=False)

    sigdf = pd.concat((sigdf.iloc[new_df.shape[0]:], new_df.iloc[-max_viz_samples:]), sort=False).reset_index(drop=True)

    for sen in sensors:
        siglines[sen].setData(sigdf[sen].values)


def update_impedances():
    global impedance_q, impdf, imp_sensors, impitem

    if simulate:
        new_data = [{s:v for s,v in zip(imp_sensors, np.abs(np.random.randn(len(imp_sensors))*1.5))} for i in range(10)]
    else:
        new_data = []
        while impedance_q.not_empty:
            try:
                sample = impedance_q.get_nowait()
                ref = sample['ref'].decode()
                if 'data' in sample:
                    new_data.append({k:sample['data'][k] for k in imp_sensors if k!=ref})
                # Other fields: 'cmf': 2.85, ref:b'Pz', 'ts':7.437
            except:
                break

    new_df = pd.DataFrame(new_data[-max_imp_samples:])
    impdf = pd.concat((impdf.iloc[new_df.shape[0]:], new_df), sort=False).reset_index(drop=True)
    impmeans = impdf.mean().values
    colors = ['g' if v<1 else 'y' if v<1.2 else 'r' for v in impmeans]
    impitem.setOpts(brushes=colors, height=impmeans)


# update all plots
def update():
    update_impedances()
    update_signals()

if __name__ == "__main__":

    acq = DataAcquisitionThread(signal_q, impedance_q, stop_event, paused_event, impedance_event, signal_event)
    if not simulate:
        acq.connect_headset()
        acq.setup_impedance()
        acq.start()
        acq.impedance_event.set()
        # to stop the thread, call acq.stop()
        # to power off device, call acq.headset.Shutdown()

    #######################
    # Set up the viz window
    #######################
    pg.setConfigOption('background', 'w')
    pg.setConfigOption('foreground', 'k')

    app = QtGui.QApplication([])
    win = QtGui.QMainWindow()
    win.resize(1920,1080)
    win.show()
    win.setWindowTitle('DSI-24 Visualizer')

    cw = QtGui.QWidget()
    win.setCentralWidget(cw)

    l = QtGui.QGridLayout()
    cw.setLayout(l)
    l.setSpacing(0)

    pltwin = pg.GraphicsLayoutWidget(show=True)
    l.addWidget(pltwin, 0, 0, 10, 1)

    def subid_changed(txt):
        global subid
        subid = txt

    subid_text = QtGui.QLineEdit(subid)
    subid_text.textChanged.connect(subid_changed)
    l.addWidget(subid_text, 0, 1)

    impedance_cb = QtGui.QCheckBox('Impedance')
    def setImpedanceMode():
        do_impedance = impedance_cb.isChecked()
        if do_impedance:
            acq.signal_event.clear()
            acq.impedance_event.set()
        else:
            acq.impedance_event.clear()
            acq.signal_event.set()

    l.addWidget(impedance_cb, 1, 1)
    impedance_cb.setChecked(True)
    impedance_cb.toggled.connect(setImpedanceMode)

    record_cb = QtGui.QCheckBox('Record')
    def setRecord():
        global record_event
        if record_cb.isChecked():
            record_event.set()
            print('START RECORDING '+ subid)
        else:
            record_event.clear()
            print('END RECORDING '+ subid)

    l.addWidget(record_cb, 2, 1)
    record_cb.setChecked(False)
    record_cb.toggled.connect(setRecord)

    reset_btn = QtGui.QPushButton('Reset Amps')
    l.addWidget(reset_btn, 3, 1)
    reset_btn.toggled.connect(acq.reset_amps)

    pause_btn = QtGui.QPushButton('Pause')
    l.addWidget(pause_btn, 8, 1)
    pause_btn.toggled.connect(acq.paused_event.set)#acq.pause)

    halt_btn = QtGui.QPushButton('Power Off')
    l.addWidget(halt_btn, 9, 1)
    def halt():
        acq.stop_event.set() #acq.stop()
        acq.headset.Shutdown()

    halt_btn.toggled.connect(halt)

    sigplots = {}
    siglines = {}
    impbars = {}

    for idx,sen in enumerate(sensors):
        row = idx // numPlots_per_row
        col = idx % numPlots_per_row
        p = pltwin.addPlot(row=row, col=col) # title=sen
        p.hideAxis('bottom')
        p.hideAxis('left')
        p.setLabel('left', sen)
        l = p.plot(sigdf[sen].values)
        sigplots[sen] = p
        siglines[sen] = l

    impplot = pltwin.addPlot(title='impedances', row=row, col=1, colspan=1)
    impitem = pg.BarGraphItem(x=np.arange(len(imp_sensors)), height=np.zeros(len(imp_sensors)), width=0.8, brush='g')
    impplot.addItem(impitem)
    ax = impplot.getAxis('bottom')
    ax.setTicks([[(i,s) for i,s in enumerate(imp_sensors)],[]])

    timer = pg.QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(50)

    QtGui.QApplication.instance().exec_()

