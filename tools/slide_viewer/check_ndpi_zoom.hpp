// Packaged regression check: compare the real tiled display against a single
// acquisition-resolution patch. No patient pixels or sidecars are written.
API int viewer_ndpi_zoom(Open o,Read r,const char* slide,int base_z){
    if(base_z!=11&&base_z!=12)return 1;
    Viewer v(o,r,nullptr);v.append(slide);if(!v.select(0)||v.count!=1)return 2;
    auto thumb=v.thumbnail(384,384);if(thumb.rgb.empty())return 3;
    int best=-1;Point center{};
    for(int j=1;j+1<thumb.h;++j)for(int i=1;i+1<thumb.w;++i){
        auto p=&thumb.rgb[(j*thumb.w+i)*3];
        int score=std::max(0,int(p[0])-p[1])+std::max(0,int(p[2])-p[1]);
        if(score>best){best=score;center={(i+.5)*thumb.step,(j+.5)*thumb.step};}
    }
    constexpr int w=897,h=607;
    const int ds=1<<(12-base_z);
    v.canvas.resize(0,0,w,h);
    for(int z:{base_z+1,base_z+2,16}){
        const int factor=1<<(z-base_z),rw=(w+factor-1)/factor+2,rh=(h+factor-1)/factor+2;
        if(v.width/ds<rw+2||v.height/ds<rh+2)return 4;
        int x=std::clamp(int(center.x/ds)-rw/2,1,int(v.width/ds)-rw);
        int y=std::clamp(int(center.y/ds)-rh/2,1,int(v.height/ds)-rh);
        RGB patch(size_t(rw)*rh*3);
        if(r((x-1)*ds,(y-1)*ds,base_z,rw,rh,0,patch.data(),v.msg.data(),v.msg.size()))return 5;
        v.it().pose={{(x+.5*w/factor)*ds,(y+.5*h/factor)*ds},z,0};v.refresh();
        if(!v.render())return 6;
        bool varied=false;
        for(int j=0;j<h;++j)for(int i=0;i<w;++i){
            double u=.5+(i+.5)/factor,t=.5+(j+.5)/factor;
            int a=int(u),b=int(t);u-=a;t-=b;
            for(int k=0;k<3;++k){
                auto at=[&](int col,int row){return patch[(row*rw+col)*3+k];};
                auto ref=std::lround((1-t)*((1-u)*at(a,b)+u*at(a+1,b))+t*((1-u)*at(a,b+1)+u*at(a+1,b+1)));
                if(v.pixels[(j*w+i)*3+k]!=ref)return 7;
                varied|=ref!=v.pixels[k];
            }
        }
        if(!varied)return 8;
    }
    return 0;
}
