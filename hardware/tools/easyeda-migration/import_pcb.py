"""usage: import_pcb.py board.epro out.kicad_pcb  -- KiCad's EasyEDA Pro PCB importer, headless"""
import sys,pcbnew
src,dst=sys.argv[1],sys.argv[2]
b=pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.EASYEDAPRO,src)
bb=b.GetBoardEdgesBoundingBox()
print(src.split('/')[-1],'fps',len(b.GetFootprints()),'tracks',len(b.GetTracks()),'zones',b.GetAreaCount(),'nets',b.GetNetCount(),'size %.1fx%.1f mm'%(bb.GetWidth()/1e6,bb.GetHeight()/1e6),flush=True)
pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP,dst,b)
print('saved',flush=True)
